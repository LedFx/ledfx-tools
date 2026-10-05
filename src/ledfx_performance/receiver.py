"""Independent structural DDP validation with bounded collector lifetimes."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import queue
import socket
import struct
import subprocess
import threading
import time
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Protocol

from ledfx_performance.data import (
    Record,
    decode_record,
    integer,
    number,
)


class Collector(Protocol):
    port: int

    def snapshot(self) -> list[int]: ...
    def interval(self) -> Record | None: ...
    def close(self) -> None: ...


class DDPReceiver:
    """Counts byte coverage, not unique frames across sequence wraps/reordering."""

    def __init__(self, pixels: int) -> None:
        self.expected_bytes = pixels * 3
        self.counts = [0, 0, 0, 0, 0]  # packets, bytes, PUSH, complete, invalid
        self.offset = 0
        self.sequence: int | None = None
        self.intact = False

    def feed(self, data: bytes) -> None:
        self.counts[0] += 1
        self.counts[1] += len(data)
        if len(data) < 10:
            self.counts[4] += 1
            self.intact = False
            return
        flags, sequence, datatype, destination, offset, length = struct.unpack(
            ">BBBBIH", data[:10]
        )
        if (
            flags & 0xC0 != 0x40
            or datatype != 0x0B
            or destination != 1
            or length != len(data) - 10
            or offset + length > self.expected_bytes
        ):
            self.counts[4] += 1
            self.intact = False
            return
        if offset == 0:
            self.sequence, self.offset, self.intact = sequence, 0, True
        self.intact = (
            self.intact and sequence == self.sequence and offset == self.offset
        )
        self.offset = offset + length
        if flags & 1:
            self.counts[2] += 1
            self.counts[3] += int(self.intact and self.offset == self.expected_bytes)
            self.intact = False


def collect_ddp(connection: Connection, pixels: int, bind: str) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        sock.bind((bind, 0))
        sock.settimeout(0.005)
        connection.send(sock.getsockname()[1])
        receiver = DDPReceiver(pixels)
        started = time.perf_counter()
        batch = 0
        while True:
            if batch == 0 and connection.poll():
                if connection.recv() == "stop":
                    break
                connection.send(
                    {
                        "counts": receiver.counts.copy(),
                        "elapsed_seconds": time.perf_counter() - started,
                        "unique_identity": False,
                    }
                )
            try:
                receiver.feed(sock.recv(65536))
                batch = (batch + 1) % 32
            except TimeoutError:
                batch = 0
    connection.close()


def counters(value: object) -> list[int]:
    if not isinstance(value, list) or len(value) != 5:
        raise ValueError("DDP receiver must return five counters")
    if any(not isinstance(n, int) or isinstance(n, bool) or n < 0 for n in value):
        raise ValueError("Invalid DDP counters")
    return [int(n) for n in value]


class DDPCollector:
    def __init__(self, pixels: int, bind: str) -> None:
        context = multiprocessing.get_context("spawn")
        self.connection, child = context.Pipe()
        self.process = context.Process(target=collect_ddp, args=(child, pixels, bind))
        self.process.start()
        self.snapshots: list[Record] = []
        child.close()
        try:
            if not self.connection.poll(10):
                raise TimeoutError("DDP receiver did not start")
            port: object = self.connection.recv()
            if not isinstance(port, int):
                raise ValueError("Invalid collector port")
            self.port = port
        except BaseException:
            self.close()
            raise

    def snapshot(self) -> list[int]:
        self.connection.send("snapshot")
        if not self.connection.poll(10):
            raise TimeoutError("DDP receiver did not report counters")
        received: object = self.connection.recv()
        if not isinstance(received, dict):
            raise ValueError("Invalid collector snapshot")
        response = decode_record(json.dumps(received))
        self.snapshots.append(response)
        return counters(response["counts"])

    def interval(self) -> Record:
        before, after = self.snapshots[-2:]
        elapsed = number(after, "elapsed_seconds") - number(before, "elapsed_seconds")
        if elapsed <= 0:
            raise ValueError("Receiver interval must be positive")
        return {
            "seconds": elapsed,
            "unique_identity": False,
            "snapshots": list(self.snapshots[-2:]),
            "cross_boundary_assembly": "Completion may begin before first snapshot",
        }

    def close(self) -> None:
        try:
            if self.process.is_alive():
                try:
                    self.connection.send("stop")
                except (OSError, EOFError):
                    pass
                self.process.join(2)
        finally:
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(2)
            self.connection.close()


class NativeCollector:
    """Optional binary supplied externally; this project never builds Rust."""

    def __init__(self, pixels: int, bind: str, binary: Path) -> None:
        self.provenance: Record = {
            "binary": str(binary.resolve()),
            "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        }
        self.snapshots: list[Record] = []
        self.messages: queue.Queue[str | None] = queue.Queue()
        self.stderr = ""
        self.process = subprocess.Popen(
            [str(binary.resolve()), "ddp-structural", str(pixels), "batched", bind],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.reader = threading.Thread(target=self._read_lines, daemon=True)
        self.errors = threading.Thread(target=self._read_errors, daemon=True)
        self.reader.start()
        self.errors.start()
        try:
            self.ready = self._read()
            self.port = integer(self.ready, "port")
        except BaseException:
            self.close()
            raise

    def _read_errors(self) -> None:
        assert self.process.stderr is not None
        self.stderr = self.process.stderr.read()

    def _read_lines(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.messages.put(line)
        self.messages.put(None)

    def _read(self) -> Record:
        try:
            line = self.messages.get(timeout=10)
        except queue.Empty as error:
            raise TimeoutError("Native DDP receiver did not respond") from error
        if line is None:
            raise RuntimeError(f"Native receiver closed: {self.stderr}")
        response = decode_record(line)
        if response.get("unique_identity") is not False:
            raise ValueError("Receiver must disclose structural identity limits")
        return response

    def _command(self, name: str) -> Record:
        assert self.process.stdin is not None
        self.process.stdin.write(name + "\n")
        self.process.stdin.flush()
        return self._read()

    def snapshot(self) -> list[int]:
        request = time.perf_counter()
        response = self._command("snapshot")
        response["controller_request_seconds"] = request
        response["controller_response_seconds"] = time.perf_counter()
        self.snapshots.append(response)
        return counters(response["counts"])

    def interval(self) -> Record:
        before, after = self.snapshots[-2:]
        elapsed = number(after, "elapsed_seconds") - number(before, "elapsed_seconds")
        if elapsed <= 0:
            raise ValueError("Receiver interval must be positive")
        return {
            "seconds": elapsed,
            "unique_identity": False,
            "snapshots": [dict(r) for r in self.snapshots[-2:]],
            "provenance": self.provenance,
            "cross_boundary_assembly": "Completion may begin before first snapshot",
        }

    def close(self) -> None:
        try:
            if self.process.poll() is None:
                self._command("stop")
                self.process.wait(timeout=5)
        except (
            OSError,
            RuntimeError,
            TimeoutError,
            ValueError,
            subprocess.TimeoutExpired,
        ):
            self.process.kill()
            self.process.wait(timeout=5)
        finally:
            self.reader.join(timeout=2)
            self.errors.join(timeout=2)
            for stream in (
                self.process.stdin,
                self.process.stdout,
                self.process.stderr,
            ):
                if stream is not None:
                    stream.close()
