"""Retain partial output and bound cleanup to processes launched by the tools."""

import os
import signal
import subprocess
import time
from pathlib import Path

import psutil

from ledfx_performance.data import Record, artifact_path, write_record


def cleanup(process: subprocess.Popen[bytes], worker_pid: int | None = None) -> None:
    children: list[psutil.Process] = []
    try:
        children = psutil.Process(process.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        pass
    if worker_pid is not None and worker_pid != process.pid:
        try:
            worker = psutil.Process(worker_pid)
            if worker not in children:
                children.append(worker)
        except psutil.NoSuchProcess:
            pass
    for child in reversed(children):
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    _, remaining = psutil.wait_procs(children, timeout=2)
    for child in remaining:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=5)
    psutil.wait_procs(children, timeout=2)


def launch(command: list[str], log: Path, cwd: Path) -> subprocess.Popen[bytes]:
    with log.open("wb") as output:
        return subprocess.Popen(
            command,
            cwd=cwd,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=os.name == "posix",
        )


def run_retained_process(
    command: list[str], prefix: Path, *, timeout: float, cwd: Path | None = None
) -> Record:
    prefix = artifact_path(prefix)
    stdout, stderr = prefix.with_suffix(".stdout"), prefix.with_suffix(".stderr")
    stdout.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    timed_out = False
    with stdout.open("wb") as out, stderr.open("wb") as err:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=out,
            stderr=err,
            start_new_session=os.name == "posix",
        )
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            cleanup(process)
    result: Record = {
        "status": "partial"
        if timed_out
        else ("failed" if process.returncode else "ok"),
        "timed_out": timed_out,
        "returncode": process.returncode,
        "elapsed_seconds": time.monotonic() - started,
        "stdout": str(stdout),
        "stderr": str(stderr),
    }
    write_record(prefix.with_suffix(".process.json"), result)
    return result
