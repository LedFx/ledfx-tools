"""Whole-app rendering, independent DDP coverage and websocket RGB throughput.

Each case runs a fresh selected checkout/environment. Profiled runs are separate
from unprofiled measurements. Receiver/client CPU is excluded from server CPU.
Unpaced controls remove render pacing only; normal effect cadence stays intact.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.metadata
import json
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import cast

import aiohttp
import psutil

from ledfx_performance.data import (
    Json,
    Record,
    command,
    decode_record,
    integer,
    number,
    read_record,
    revision,
    source_manifest,
    text,
    working_tree,
    write_record,
)
from ledfx_performance.options import Options, parse_args
from ledfx_performance.process import cleanup, launch
from ledfx_performance.receiver import Collector, DDPCollector, NativeCollector


def validate_frame(event: Record, expected_pixels: int) -> list[int]:
    shape = event.get("shape")
    if (
        not isinstance(shape, list)
        or len(shape) != 2
        or any(not isinstance(n, int) or isinstance(n, bool) or n <= 0 for n in shape)
    ):
        raise ValueError(f"Invalid preview shape: {shape}")
    dimensions = cast(list[int], shape)
    if dimensions[0] * dimensions[1] != expected_pixels:
        raise ValueError(f"Unexpected pixel shape: {shape}")
    pixels = text(event, "pixels")
    if len(base64.b64decode(pixels, validate=True)) != expected_pixels * 3:
        raise ValueError("Truncated RGB preview")
    return dimensions


def free_port(bind: str) -> int:
    with socket.socket() as sock:
        sock.bind((bind, 0))
        return int(sock.getsockname()[1])


def cpu_observation(process: psutil.Process) -> tuple[float, float]:
    before = time.perf_counter()
    cpu = sum(process.cpu_times()[:2])
    after = time.perf_counter()
    return cpu, (before + after) / 2


def reserve_output(path: Path) -> None:
    owned = [
        path,
        *[
            Path(str(path) + suffix)
            for suffix in (
                ".metadata.json",
                ".summary.json",
                ".catalog.json",
                ".failures.jsonl",
                ".artifacts",
            )
        ],
    ]
    if any(item.exists() for item in owned):
        raise FileExistsError(f"Refusing to overwrite results or diagnostics: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Reserve even a completely failed campaign, before creating its sidecars.
    with path.open("x", encoding="utf-8"):
        pass


def summarize(times: list[list[float]], start: float, end: float) -> Record:
    values = sorted(d * 1000 for t, d in times if start <= t < end)
    return {
        "count": len(values),
        "fps": round(len(values) / (end - start), 2),
        "mean_ms": round(statistics.mean(values), 3) if values else 0,
        "p95_ms": round(values[int(0.95 * (len(values) - 1))], 3) if values else 0,
    }


def worker_command(
    args: Options, directory: Path, port: int, sink: int, stream: str
) -> list[str]:
    arguments = [
        "--worker",
        "--repo",
        str(args.repo),
        "--python",
        str(args.python),
        "--directory",
        str(directory),
        "--port",
        str(port),
        "--sink-port",
        str(sink),
        "--bind",
        args.bind,
        "--pixels",
        str(args.pixels),
        "--vis-pixels",
        str(args.pixels if stream == "full" else 81),
        "--device",
        args.device,
        "--refresh-rate",
        str(args.refresh_rate),
        "--preview-fps",
        str(args.preview_fps),
        "--effect-speed",
        str(args.effect_speed),
        "--effect",
        args.effect,
        "--rows",
        str(args.rows),
        "--effect-config",
        args.effect_config,
        "--seed",
        str(args.seed),
        "--output",
        str(args.output),
    ]
    for name in ("unpaced", "unpaced_preview", "synthetic_audio", "fixtures"):
        if getattr(args, name):
            arguments.append("--" + name.replace("_", "-"))
    if args.profile:
        arguments.extend(["--profile", args.profile])
    if args.sampling:
        arguments.extend(["--sampling", args.sampling])
        launcher = Path(__file__).resolve().with_name("entry.py")
        return [
            str(args.python),
            "-m",
            "profiling.sampling",
            "run",
            "--all-threads",
            "--mode",
            args.sampling,
            "--binary",
            "-o",
            str(directory / "sampling.bin"),
            str(launcher),
            "ledfx_performance.pixel",
            *arguments,
        ]
    return command(args.python, "ledfx_performance.pixel", arguments)


async def run_one(args: Options, stream: str, repeat: int) -> Record:
    with tempfile.TemporaryDirectory(prefix="ledfx-pixels-") as temporary:
        directory = Path(temporary)
        proc: subprocess.Popen[bytes] | None = None
        worker_pid: int | None = None
        collector: Collector | None = None
        try:
            collector = (
                NativeCollector(args.pixels, args.bind, args.receiver)
                if args.receiver
                else DDPCollector(args.pixels, args.bind)
            )
            port = free_port(args.bind)
            proc = launch(
                worker_command(args, directory, port, collector.port, stream),
                directory / "server.log",
                directory,
            )
            ready_path = directory / "ready.json"
            deadline = time.monotonic() + min(60, args.case_timeout)
            while not ready_path.exists():
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError((directory / "server.log").read_text())
                await asyncio.sleep(0.1)
            ready = read_record(ready_path)
            worker_pid = integer(ready, "pid")
            process = psutil.Process(worker_pid)
            ws_count = ws_bytes = ws_changed = 0
            previous: str | None = None
            shape: list[int] | None = None
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            ) as session:
                ws: aiohttp.ClientWebSocketResponse | None = None
                url = f"http://{args.bind}:{port}"
                if stream != "none":
                    ws = await session.ws_connect(
                        url + "/api/websocket",
                        max_msg_size=max(1024 * 1024, args.pixels * 4 + 4096),
                    )
                    await ws.send_json(
                        {
                            "id": 1,
                            "type": "subscribe_event",
                            "event_type": "visualisation_update",
                            "event_filter": {"vis_id": "benchmark"},
                        }
                    )

                async def receive_for(duration: float, measure: bool) -> None:
                    nonlocal ws_count, ws_bytes, ws_changed, previous, shape
                    until = time.perf_counter() + duration
                    if ws is None:
                        await asyncio.sleep(duration)
                        return
                    while time.perf_counter() < until:
                        try:
                            msg = await ws.receive(
                                timeout=max(0.01, until - time.perf_counter())
                            )
                        except TimeoutError:
                            break
                        if msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            raise RuntimeError("Websocket closed during measurement")
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        payload: object = msg.data
                        if not isinstance(payload, str):
                            raise ValueError("Websocket TEXT payload must be text")
                        event = decode_record(payload)
                        if event.get("event_type") != "visualisation_update":
                            continue
                        # Validate warmup frames as well as measured frames.
                        shape = validate_frame(event, integer(ready, "preview_pixels"))
                        if measure:
                            ws_count += 1
                            ws_bytes += len(payload.encode())
                            pixels = text(event, "pixels")
                            ws_changed += int(pixels != previous)
                            previous = pixels

                await receive_for(args.warmup, False)
                async with session.post(url + "/benchmark/start") as response:
                    response.raise_for_status()
                counts0 = collector.snapshot()
                cpu0, cpu_start = cpu_observation(process)
                begin = time.perf_counter()
                await receive_for(args.seconds, True)
                end = time.perf_counter()
                cpu1, cpu_end = cpu_observation(process)
                counts1 = collector.snapshot()
                interval = collector.interval()
                receiver_seconds = (
                    number(interval, "seconds") if interval else end - begin
                )
                rss = process.memory_info().rss
                async with session.post(url + "/benchmark/stop") as response:
                    response.raise_for_status()
                if ws:
                    await ws.close()
                async with session.post(url + "/api/power", json={}) as response:
                    response.raise_for_status()
            await asyncio.to_thread(proc.wait, 30)
            if proc.returncode:
                raise RuntimeError((directory / "server.log").read_text())
            server_log = (directory / "server.log").read_text()
            if any(
                marker in server_log
                for marker in (
                    "frame render failed",
                    "Exception in thread",
                    "Exception in core event loop",
                )
            ):
                raise RuntimeError(f"Worker failed during benchmark:\n{server_log}")
            raw_metrics = read_record(directory / "metrics.json")
            result: Record = {
                "platform": sys.platform,
                "python": ready["worker_python"],
                "loop": "standard",
                "repeat": repeat,
                "device": args.device,
                "pixels": args.pixels,
                "refresh_rate": args.refresh_rate,
                "effective_refresh_rate": ready["refresh_rate"],
                "bind": args.bind,
                "receiver_mode": "native" if args.receiver else "python-process",
                "receiver": interval,
                "receiver_identity": collector.provenance["sha256"]
                if isinstance(collector, NativeCollector)
                else hashlib.sha256(
                    Path(__file__).with_name("receiver.py").read_bytes()
                ).hexdigest(),
                "benchmark_identity": args.benchmark_identity,
                "seed": args.seed,
                "unpaced": args.unpaced,
                "unpaced_scope": "render-only" if args.unpaced else "none",
                "unpaced_preview": args.unpaced_preview,
                "preview_fps": args.preview_fps,
                "effect_speed": args.effect_speed,
                "effect_id": args.effect,
                "rows": ready["rows"],
                "effect_config": ready["effect_config"],
                "config_overrides": decode_record(args.effect_config),
                "synthetic_audio": args.synthetic_audio,
                "fixtures": args.fixtures,
                "stream": stream,
                "profiled": f"sampling-{args.sampling}"
                if args.sampling
                else args.profile or False,
                "loop_class": ready["loop"],
                "seconds": end - begin,
                "server_cpu_pct": (cpu1 - cpu0) / (cpu_end - cpu_start) * 100,
                "server_cpu_observation_seconds": cpu_end - cpu_start,
                "rss_mb": rss / 1e6,
                "ws_fps": ws_count / (end - begin),
                "ws_changed_fps": ws_changed / (end - begin),
                "ws_MB_s": ws_bytes / (end - begin) / 1e6,
                "ws_shape": list(shape) if shape else None,
                "ddp_MB_s": (counts1[1] - counts0[1]) / receiver_seconds / 1e6,
                "ddp_push_fps": (counts1[2] - counts0[2]) / receiver_seconds,
                "ddp_complete_fps": (counts1[3] - counts0[3]) / receiver_seconds,
                "ddp_invalid_packets": counts1[4] - counts0[4],
                "ddp_packets": counts1[0] - counts0[0],
                "ddp_completion_scope": (
                    "Structural coverage, not unique frame identity "
                    "across reorder/sequence wraps"
                ),
                "ddp_source_sha256": ready["ddp_source_sha256"],
                "worker_packages": ready["worker_packages"],
                "installed_sender_files": ready["installed_sender_files"],
                "fixture_files": ready["fixture_files"],
            }
            for key, values in raw_metrics.items():
                if not isinstance(values, list) or any(
                    not isinstance(pair, list)
                    or len(pair) != 2
                    or any(not isinstance(n, (int, float)) for n in pair)
                    for pair in values
                ):
                    raise ValueError(f"Invalid timing metric {key}")
                result[key] = summarize(cast(list[list[float]], values), begin, end)
            assemble = cast(Record, result["assemble"])
            if not number(assemble, "count"):
                raise RuntimeError("No rendered frames during measurement")
            if ready["reactive"] and (
                not number(cast(Record, result["audio"]), "count")
                or not number(cast(Record, result["audio_update"]), "count")
            ):
                raise RuntimeError(
                    "Reactive measurement had no real audio/DSP callbacks"
                )
            if stream != "none" and not ws_count:
                raise RuntimeError("No validated websocket frames")
            if args.device == "ddp" and (
                counts1[3] == counts0[3] or result["ddp_invalid_packets"]
            ):
                raise RuntimeError("No complete DDP frames or malformed packets")
            if args.sampling and not (directory / "sampling.bin").is_file():
                raise RuntimeError("Sampling completed without a recording")
            with args.output.open("a", encoding="utf-8") as output:
                output.write(json.dumps(result) + "\n")
            print(json.dumps(result), flush=True)
            return result
        finally:
            if proc is not None:
                await asyncio.to_thread(cleanup, proc, worker_pid)
            if collector is not None:
                collector.close()
                if isinstance(collector, NativeCollector):
                    write_record(
                        directory / "receiver.json",
                        {
                            "ready": collector.ready,
                            "snapshots": list(collector.snapshots),
                            "stderr": collector.stderr,
                            "returncode": collector.process.returncode,
                            "provenance": collector.provenance,
                        },
                    )
            destination = (
                Path(str(args.output) + ".artifacts")
                / args.effect
                / f"{repeat}-{stream}"
            )
            destination.mkdir(parents=True, exist_ok=True)
            for name in (
                "server.log",
                "receiver.json",
                "metrics.json",
                "ready.json",
                "main.prof",
                "render.prof",
                "effect.prof",
                "sampling.bin",
            ):
                source = directory / name
                if source.exists():
                    shutil.copy2(source, destination / name)


def canonical_value(value: Json, fixture_paths: dict[str, str]) -> Json:
    if isinstance(value, str):
        return fixture_paths.get(value, value)
    if isinstance(value, list):
        return [canonical_value(item, fixture_paths) for item in value]
    if isinstance(value, dict):
        return {
            key: canonical_value(item, fixture_paths) for key, item in value.items()
        }
    return value


def scenario_key(row: Record) -> str:
    keys = (
        "platform",
        "python",
        "loop",
        "device",
        "pixels",
        "refresh_rate",
        "preview_fps",
        "effect_speed",
        "effect_id",
        "rows",
        "effect_config",
        "stream",
        "profiled",
        "synthetic_audio",
        "fixtures",
        "unpaced",
        "unpaced_scope",
        "unpaced_preview",
        "seed",
        "receiver_mode",
        "receiver_identity",
        "benchmark_identity",
    )
    fixture_paths: dict[str, str] = {}
    files = row.get("fixture_files", {})
    if isinstance(files, dict):
        for name, fixture in files.items():
            if not isinstance(fixture, dict):
                raise ValueError("Invalid generated fixture identity")
            identity = f"fixture:{name}:{text(fixture, 'sha256')}"
            fixture_paths[text(fixture, "path")] = identity
            fixture_paths[Path(text(fixture, "path")).as_uri()] = identity
    return json.dumps(
        {key: canonical_value(row.get(key), fixture_paths) for key in keys},
        sort_keys=True,
    )


def group_rows(rows: list[Record]) -> dict[str, list[Record]]:
    groups: dict[str, list[Record]] = {}
    for row in rows:
        groups.setdefault(scenario_key(row), []).append(row)
    return groups


def throughput(row: Record) -> dict[str, float]:
    values = {"render_fps": number(cast(Record, row["assemble"]), "fps")}
    if row["device"] == "ddp":
        values["ddp_complete_fps"] = number(row, "ddp_complete_fps")
    if row["stream"] != "none":
        values["ws_fps"] = number(row, "ws_fps")
    return values


def check_regression(
    previous: list[Record], current: list[Record], tolerance: float
) -> list[str]:
    before, after = group_rows(previous), group_rows(current)
    failures: list[str] = []
    for key, rows in after.items():
        if key not in before:
            failures.append(f"No matching baseline for {key}")
            continue
        for metric in throughput(rows[0]):
            a = statistics.median(throughput(row)[metric] for row in before[key])
            b = statistics.median(throughput(row)[metric] for row in rows)
            if a > 0 and b < a * (1 - tolerance):
                failures.append(f"{metric}: {a:.2f} -> {b:.2f} ({key})")
    return failures


async def driver(args: Options) -> None:
    effects = args.effects.split(",")
    if args.effects == "all":
        completed = subprocess.run(
            command(
                args.python, "ledfx_performance.fixtures", ["--repo", str(args.repo)]
            ),
            text=True,
            capture_output=True,
            check=True,
            timeout=args.case_timeout,
        )
        descriptions = decode_record(completed.stdout)
        write_record(Path(str(args.output) + ".catalog.json"), dict(descriptions))
        effects = list(descriptions)
    rows: list[Record] = []
    failures: list[str] = []
    for repeat in range(args.repeats):
        for effect in effects:
            for stream in args.streams.split(","):
                try:
                    rows.append(
                        await asyncio.wait_for(
                            run_one(replace(args, effect=effect), stream, repeat),
                            args.case_timeout,
                        )
                    )
                except Exception as error:
                    failure = f"{effect}/{stream}/{repeat}: {error}"
                    failures.append(failure)
                    with Path(str(args.output) + ".failures.jsonl").open("a") as output:
                        output.write(
                            json.dumps(
                                {
                                    "effect": effect,
                                    "stream": stream,
                                    "repeat": repeat,
                                    "error": repr(error),
                                }
                            )
                            + "\n"
                        )
                    if not args.keep_going:
                        raise
    summary: Record = {
        key: {
            metric: statistics.median(throughput(row)[metric] for row in values)
            for metric in throughput(values[0])
        }
        for key, values in group_rows(rows).items()
    }
    write_record(Path(str(args.output) + ".summary.json"), summary)
    if args.baseline:
        previous = [
            decode_record(line) for line in args.baseline.read_text().splitlines()
        ]
        failures.extend(check_regression(previous, rows, args.max_regression))
    if failures:
        raise SystemExit("\n".join(failures))


def main() -> None:
    args = parse_args()
    if args.worker:
        from ledfx_performance.worker import worker

        worker(args)
        return
    reserve_output(args.output)
    benchmark_sources = source_manifest(
        Path(__file__).resolve().parent,
        Path(__file__).resolve().parent,
    )
    args.benchmark_identity = hashlib.sha256(
        json.dumps(benchmark_sources, sort_keys=True).encode()
    ).hexdigest()
    packages: Record = {}
    for name in ("numpy", "aiohttp", "psutil"):
        packages[name] = importlib.metadata.version(name)
    metadata: Record = {
        "controller_python": sys.version,
        "worker_python_path": str(args.python),
        "repo": str(args.repo),
        "revision": revision(args.repo),
        "working_tree": working_tree(args.repo),
        "app_source_files": source_manifest(args.repo, args.repo / "ledfx"),
        "app_dependency_files": {
            name: hashlib.sha256((args.repo / name).read_bytes()).hexdigest()
            for name in ("pyproject.toml", "uv.lock")
            if (args.repo / name).exists()
        },
        "benchmark_source_files": benchmark_sources,
        "benchmark_config_files": {
            name: hashlib.sha256(
                (Path(__file__).resolve().parents[2] / name).read_bytes()
            ).hexdigest()
            for name in ("pyproject.toml", "uv.lock")
            if (Path(__file__).resolve().parents[2] / name).is_file()
        },
        "controller_packages": packages,
        "arguments": {
            key: str(value) if isinstance(value, Path) else cast(Json, value)
            for key, value in asdict(args).items()
        },
        "notes": (
            "Synthetic loopback; server CPU excludes receiver/profiler. "
            "RSS is not peak/leak evidence; changed previews differ from repeats."
        ),
    }
    write_record(Path(str(args.output) + ".metadata.json"), metadata)
    print(f"Results: {args.output}", file=sys.stderr)
    asyncio.run(driver(args))


if __name__ == "__main__":
    main()
