"""One fresh app process: instrument render/audio and serve validated previews."""

from __future__ import annotations

import asyncio
import cProfile
import hashlib
import importlib
import importlib.metadata
import json
import logging
import math
import os
import random
import sys
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Protocol, cast
from unittest.mock import patch

import numpy as np
from aiohttp import web
from pydantic import BaseModel

from ledfx_performance.data import (
    Record,
    decode_record,
    select_checkout,
    sender_manifest,
    write_record,
)
from ledfx_performance.fixtures import (
    InputStream,
    class_name,
    exported,
    install_audio,
    is_kind,
    local_config,
    matrix_rows,
)
from ledfx_performance.options import Options


class Virtual(Protocol):
    rows: int
    refresh_rate: int


class Virtuals(Protocol):
    def get(self, identifier: str) -> Virtual: ...
    def set_effect(
        self,
        identifier: str,
        effect: str,
        config: BaseModel,
        *,
        store: bool,
    ) -> None: ...


class Effects(Protocol):
    def get_class(self, name: str) -> type[object]: ...


class Devices(Protocol):
    async def add_new_device(self, name: str, config: Record) -> object: ...


class Listener(Protocol):
    callback: Callable[..., object]


class Events(Protocol):
    _listeners: dict[str, list[Listener]]

    def fire_event(self, event: object) -> None: ...


class Http(Protocol):
    app: web.Application


class Core(Protocol):
    config: object
    loop: asyncio.AbstractEventLoop
    events: Events
    virtuals: Virtuals
    devices: Devices
    effects: Effects
    http: Http
    exit_code: int | None

    def setup_visualisation_events(self) -> None: ...
    async def async_start(self) -> None: ...
    async def async_stop(self, exit_code: int) -> None: ...


class CoreFactory(Protocol):
    def __call__(
        self,
        directory: str,
        *,
        host: str,
        port: int,
        offline_mode: bool,
    ) -> Core: ...


class Configurable(Protocol):
    def config_model(self) -> type[BaseModel]: ...


class FrontendEvent(Protocol):
    def __call__(
        self,
        identifier: str,
        pixels: np.ndarray[tuple[int, ...], np.dtype[np.uint8]],
        shape: tuple[int, int],
        source: str,
    ) -> object: ...


class UnpacedRenderClock:
    """Yield only the target render thread; effect cadence remains unchanged."""

    def __getattr__(self, name: str) -> object:
        return cast(object, getattr(time, name))

    def sleep(self, seconds: float) -> None:
        import threading

        selected = threading.current_thread().name == "Virtual: benchmark"
        time.sleep(0 if selected else seconds)


def disable_service(owner: object) -> None:
    """Keep host media services out of a synthetic process."""


def worker(args: Options) -> None:
    assert args.directory is not None
    directory = args.directory
    select_checkout(args.repo)
    random.seed(args.seed)
    np.random.seed(args.seed)
    if args.sampling and sys.version_info < (3, 15):
        raise ValueError("Sampling requires Python 3.15+ in the worker environment")
    if args.synthetic_audio:
        install_audio()
    if args.unpaced:
        setattr(importlib.import_module("ledfx.virtuals"), "time", UnpacedRenderClock())
    metrics: dict[str, list[list[float]]] = {
        name: []
        for name in (
            "assemble",
            "flush",
            "visualise",
            "effect",
            "audio",
            "audio_update",
            "loop_lag",
        )
    }
    profiles = {name: cProfile.Profile() for name in ("main", "render", "effect")}
    profiling = False

    def timed(fn: Callable[..., object], key: str) -> Callable[..., object]:
        def call(*arguments: object, **keywords: object) -> object:
            if key == "assemble" and getattr(arguments[0], "id") != "benchmark":
                return fn(*arguments, **keywords)
            start = time.perf_counter()
            selected = "render" if key in ("assemble", "flush") else key
            profile = profiles.get(selected)
            if profiling and profile is not None and args.profile == selected:
                result: object = profile.runcall(fn, *arguments, **keywords)
            else:
                result = fn(*arguments, **keywords)
            metrics[key].append([start, time.perf_counter() - start])
            return result

        return call

    def instrument(owner: object, attribute: str, key: str) -> None:
        fn = cast(Callable[..., object], getattr(owner, attribute))
        setattr(owner, attribute, timed(fn, key))

    instrument(exported("ledfx.virtuals", "Virtual"), "assemble_frame", "assemble")
    ddp = exported("ledfx.devices.ddp", "DDPDevice")
    instrument(ddp, "flush", "flush")
    audio = exported("ledfx.effects.audio", "AudioInputSource")
    instrument(audio, "_audio_sample_callback", "audio")
    factory = cast(CoreFactory, exported("ledfx.core", "LedFxCore"))
    logging.basicConfig(level=logging.WARNING)
    with patch("ledfx.core.asyncio.new_event_loop", asyncio.new_event_loop):
        # The baseline may prefer uvloop; force the same standard loop in both apps.
        with patch.dict(sys.modules, {"uvloop": None, "winloop": None}):
            core = factory(
                str(directory), host=args.bind, port=args.port, offline_mode=True
            )
    setattr(core.config, "visualisation_fps", args.preview_fps)
    if args.unpaced_preview:
        object.__setattr__(core.config, "visualisation_fps", 1_000_000)
    object.__setattr__(core.config, "visualisation_maxlen", args.vis_pixels)
    setattr(core.config, "transmission_mode", "compressed")
    core.setup_visualisation_events()
    for kind in ("virtual_update", "device_update"):
        for listener in core.events._listeners.get(kind, []):
            listener.callback = timed(listener.callback, "visualise")
    setattr(core, "_start_audio_device_monitor", lambda: None)
    for module, name in (
        ("ledfx.nowplaying.providers.mpris", "MPRISNowPlayingProvider"),
        ("ledfx.nowplaying.providers.smtc", "SMTCNowPlayingProvider"),
    ):
        setattr(exported(module, name), "start", disable_service)

    async def begin_measurement(request: web.Request) -> web.Response:
        nonlocal profiling
        profiling = bool(args.profile)
        if args.profile == "main":
            profiles["main"].enable()
        return web.json_response({"started": True})

    async def end_measurement(request: web.Request) -> web.Response:
        nonlocal profiling
        profiling = False
        profiles["main"].disable()
        return web.json_response({"stopped": True})

    core.http.app.router.add_post("/benchmark/start", begin_measurement)
    core.http.app.router.add_post("/benchmark/stop", end_measurement)

    async def measure_loop_lag() -> None:
        while True:
            start = time.perf_counter()
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            metrics["loop_lag"].append([now, max(0, now - start - 0.01)])

    async def feed_frontend() -> None:
        event = cast(
            FrontendEvent, exported("ledfx.events", "FrontendVisualiserDataEvent")
        )
        frame = np.zeros((128, 128, 3), dtype=np.uint8)
        n = 0
        while True:
            frame[:, :, 0] = n % 256
            frame[:, :, 1] = np.arange(128, dtype=np.uint8)
            core.events.fire_event(
                event("benchmark", frame.copy(), (128, 128), "benchmark")
            )
            n += 7
            await asyncio.sleep(1 / 60)

    def set_effect(identifier: str, name: str, config: Record) -> Record:
        cls = cast(Configurable, core.effects.get_class(name))
        model = cls.config_model().model_validate(config)
        core.virtuals.set_effect(
            identifier,
            name,
            model,
            store=False,
        )
        return decode_record(model.model_dump_json())

    tasks: list[asyncio.Task[None]] = []

    def background(coroutine: Coroutine[object, object, None]) -> None:
        tasks.append(core.loop.create_task(coroutine))

    async def start() -> None:
        try:
            await core.async_start()
            config: Record = {
                "name": "Benchmark",
                "pixel_count": args.pixels,
                "refresh_rate": args.refresh_rate,
            }
            if args.device == "ddp":
                config.update(ip_address=args.bind, port=args.sink_port)
            await core.devices.add_new_device(args.device, config)
            cls = core.effects.get_class(args.effect)
            temporal = is_kind(cls, "ledfx.effects.temporal", "TemporalEffect")
            reactive = is_kind(cls, "ledfx.effects.audio", "AudioReactiveEffect")
            if reactive and not args.synthetic_audio:
                raise ValueError("Audio-reactive effects require --synthetic-audio")
            rows = args.rows or (
                matrix_rows(args.pixels)
                if is_kind(cls, "ledfx.effects.twod", "Twod")
                or class_name(cls, "CATEGORY") == "Matrix"
                else 1
            )
            core.virtuals.get("benchmark").rows = rows
            instrument(cls, "effect_loop" if temporal else "_render", "effect")
            if reactive:
                instrument(cls, "audio_data_updated", "audio_update")
            effect_config: Record = (
                local_config(args.effect, directory) if args.fixtures else {}
            )
            if args.fixtures and args.effect in {"blender", "radial"}:
                await core.devices.add_new_device(
                    "dummy",
                    {"name": "Fixture", "pixel_count": 1024},
                )
                core.virtuals.get("fixture").rows = 32
                set_effect("fixture", "rainbow", {"speed": 6})
                if args.effect == "blender":
                    effect_config.update(
                        mask="fixture", foreground="fixture", background="fixture"
                    )
                else:
                    effect_config.update(source_virtual="fixture")
            if args.effect == "rainbow":
                effect_config.update(speed=args.effect_speed, blur=0)
            effect_config.update(decode_record(args.effect_config))
            effect_config = set_effect("benchmark", args.effect, effect_config)
            stream: object = getattr(audio, "_stream", None)
            if (
                reactive
                and args.synthetic_audio
                and not isinstance(stream, InputStream)
            ):
                raise RuntimeError("Requested synthetic audio stream did not activate")
            if args.synthetic_audio and stream is not None:
                cast(InputStream, stream).begin()
            if args.fixtures and args.effect == "frontend":
                background(feed_frontend())
            fit = cast(
                Callable[[int, tuple[int, int], int], tuple[tuple[int, int], int]],
                exported("ledfx.utils", "shape_to_fit_len"),
            )
            source = Path(
                cast(
                    str,
                    getattr(importlib.import_module("ledfx.devices.ddp"), "__file__"),
                )
            )
            ready: Record = {
                "loop": str(type(core.loop)),
                "pid": os.getpid(),
                "refresh_rate": core.virtuals.get("benchmark").refresh_rate,
                "rows": rows,
                "effect_config": effect_config,
                "preview_pixels": math.prod(
                    fit(
                        args.vis_pixels,
                        (rows, args.pixels // rows),
                        args.pixels,
                    )[0]
                )
                if args.pixels > args.vis_pixels
                else args.pixels,
                "ddp_source": str(source),
                "ddp_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "worker_python": sys.version,
            }
            versions: Record = {}
            for package in (
                "numpy",
                "aiohttp",
                "pillow",
                "pydantic",
                "psutil",
                "ledfx-senders",
                "aubio-ledfx",
                "pyfastnoiselite-ledfx",
                "samplerate-ledfx",
                "audio-hotplug",
            ):
                try:
                    versions[package] = importlib.metadata.version(package)
                except importlib.metadata.PackageNotFoundError:
                    versions[package] = None
            ready["worker_packages"] = versions
            ready["reactive"] = reactive
            ready["installed_sender_files"] = sender_manifest()
            ready["fixture_files"] = {
                path.name: {
                    "path": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                for path in sorted(directory.glob("fixture.*"))
            }
            write_record(directory / "ready.json", ready)
            background(measure_loop_lag())
        except BaseException:
            logging.getLogger(__name__).exception("Benchmark startup failed")
            await core.async_stop(1)

    background(start())
    try:
        core.loop.run_forever()
    finally:
        profiles["main"].disable()
        (directory / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
        if args.profile:
            profiles[args.profile].dump_stats(str(directory / f"{args.profile}.prof"))
        core.loop.close()
    # /api/power reports 3 for a requested normal shutdown.
    if core.exit_code not in (0, 3):
        raise SystemExit(core.exit_code or 1)
