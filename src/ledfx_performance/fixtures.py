"""Local inputs replace hardware, retaining LedFx's real effect/DSP algorithms."""

from __future__ import annotations

import importlib
import math
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol, cast

import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageDraw

from ledfx_performance.data import Record

AudioCallback = Callable[[NDArray[np.float32], int, None, None], None]


class Registry(Protocol):
    def classes(self) -> dict[str, type[object]]: ...


class RegistryFactory(Protocol):
    def __call__(self, owner: object) -> Registry: ...


def exported(module: str, name: str) -> object:
    return cast(object, getattr(importlib.import_module(module), name))


def class_name(cls: type[object], attr: str) -> str:
    value: object = getattr(cls, attr)
    if not isinstance(value, str):
        raise ValueError(f"Effect {attr} must be text")
    return value


def is_kind(cls: type[object], module: str, name: str) -> bool:
    return issubclass(cls, cast(type[object], exported(module, name)))


def catalog() -> dict[str, Record]:
    factory = cast(RegistryFactory, exported("ledfx.effects", "Effects"))
    registry = factory(SimpleNamespace(audio=None))
    return {
        name: {
            "name": class_name(cls, "NAME"),
            "matrix": is_kind(cls, "ledfx.effects.twod", "Twod")
            or class_name(cls, "CATEGORY") == "Matrix",
            "audio": is_kind(cls, "ledfx.effects.audio", "AudioReactiveEffect"),
            "temporal": is_kind(cls, "ledfx.effects.temporal", "TemporalEffect"),
        }
        for name, cls in sorted(registry.classes().items())
    }


def matrix_rows(pixels: int) -> int:
    if pixels <= 0:
        raise ValueError("Pixel count must be positive")
    rows = math.isqrt(pixels)
    while pixels % rows:
        rows -= 1
    return rows


def audio_signal(sample_rate: int = 30000, seconds: int = 8) -> NDArray[np.float32]:
    """Seeded 120 BPM drum pulses, changing chords, sweep and broadband noise."""
    t = np.arange(sample_rate * seconds) / sample_rate
    beat = t % 0.5
    rng = np.random.default_rng(42)
    notes = np.array([220, 277.18, 329.63, 440])[(t.astype(int) // 2) % 4]
    signal = (
        0.40 * np.sin(2 * np.pi * 65 * t) * np.exp(-beat * 35)
        + 0.16 * np.sin(2 * np.pi * notes * t)
        + 0.10 * np.sin(2 * np.pi * notes * 1.5 * t)
        + 0.08 * np.sin(2 * np.pi * (1000 * t + 160 * t * t))
        + 0.10 * rng.standard_normal(len(t)) * np.exp(-(t % 0.25) * 80)
    )
    return np.clip(signal, -1, 1).astype(np.float32)


class InputStream:
    def __init__(
        self,
        *,
        callback: AudioCallback,
        samplerate: int,
        blocksize: int,
        **kwargs: object,
    ) -> None:
        self.callback, self.samplerate, self.blocksize = callback, samplerate, blocksize
        self.signal = audio_signal(samplerate)
        self.stopped = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        """Defer PCM until audio analysis and effect activation finish."""

    def begin(self) -> None:
        def feed() -> None:
            offset = 0
            deadline = time.perf_counter()
            while not self.stopped.is_set():
                frame = self.signal[offset : offset + self.blocksize].copy()
                offset = (offset + self.blocksize) % len(self.signal)
                if len(frame) != self.blocksize:
                    offset = 0
                    continue
                self.callback(frame, self.blocksize, None, None)
                deadline = max(
                    deadline + self.blocksize / self.samplerate, time.perf_counter()
                )
                self.stopped.wait(max(0, deadline - time.perf_counter()))

        self.thread = threading.Thread(target=feed, name="Benchmark PCM", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stopped.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(5)

    close = stop


def install_audio() -> None:
    source = exported("ledfx.effects.audio", "AudioInputSource")
    setattr(
        source,
        "query_devices",
        staticmethod(
            lambda: (
                {
                    "name": "Benchmark PCM",
                    "hostapi": 0,
                    "max_input_channels": 1,
                    "default_samplerate": 30000,
                },
            )
        ),
    )
    setattr(
        source,
        "query_hostapis",
        staticmethod(
            lambda: (
                {
                    "name": "Benchmark",
                    "default_input_device": 0,
                },
            )
        ),
    )
    setattr(source, "default_device_index", staticmethod(lambda: 0))
    setattr(source, "_audio", SimpleNamespace(InputStream=InputStream))


def local_config(effect: str, directory: Path) -> Record:
    if effect == "random_flash":
        return {"hit_probability_per_sec": 1.0}
    if effect in {"gifplayer", "keybeat2d", "imagespin"}:
        path = directory / "fixture.gif"
        frames: list[Image.Image] = []
        for n in range(16):
            frame = Image.new("RGB", (128, 128), (n * 15, 40, 255 - n * 15))
            ImageDraw.Draw(frame).rectangle(
                (n * 6, 16, n * 6 + 24, 110), fill=(255, 200, 20)
            )
            frames.append(frame)
        frames[0].save(
            path, save_all=True, append_images=frames[1:], duration=50, loop=0
        )
        if effect == "imagespin":
            path = directory / "fixture.png"
            frames[0].resize((1024, 1024)).save(path)
            return {"image_source": str(path), "spin": True}
        return {"image_location": str(path)}
    if effect == "clone":
        module = importlib.import_module("ledfx.effects.clone")

        class Screen:
            def __init__(self) -> None:
                self.monitors = [{"top": 0, "left": 0}] * 5

            def close(self) -> None:
                """Match current Clone capture ownership during shutdown."""

            def grab(self, area: dict[str, int]) -> SimpleNamespace:
                color = int(time.perf_counter() * 60) % 256
                frame = Image.new(
                    "RGB", (area["width"], area["height"]), (color, 80, 255 - color)
                )
                return SimpleNamespace(
                    size=frame.size, bgra=frame.tobytes("raw", "BGRX")
                )

        setattr(getattr(module, "mss"), "mss", Screen)
    return {}


def main() -> None:
    import argparse
    import json

    from ledfx_performance.data import select_checkout

    parser = argparse.ArgumentParser(description="List a selected app's effect catalog")
    parser.add_argument("--repo", type=Path, required=True)
    args = parser.parse_args()
    select_checkout(Path(args.repo))
    print(json.dumps(catalog()))


if __name__ == "__main__":
    main()
