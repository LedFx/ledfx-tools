"""Explicit application/environment selection and bounded workload settings."""

from __future__ import annotations

import argparse
import ipaddress
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ledfx_performance.data import artifact_path, checkout, decode_record


@dataclass
class Options:
    repo: Path = field(default_factory=Path)
    python: Path = field(default_factory=lambda: Path(sys.executable))
    output: Path = field(default_factory=lambda: Path("artifacts/pixels.jsonl"))
    receiver: Path | None = None
    benchmark_identity: str = ""
    pixels: int = 50000
    rows: int = 0
    effects: str = "rainbow"
    effect: str = "rainbow"
    effect_config: str = "{}"
    streams: str = "none,full"
    seconds: float = 8
    warmup: float = 2
    repeats: int = 3
    bind: str = "127.0.0.1"
    device: str = "ddp"
    refresh_rate: int = 60
    preview_fps: int = 60
    effect_speed: float = 6
    seed: int = 2058
    synthetic_audio: bool = False
    fixtures: bool = False
    unpaced: bool = False
    unpaced_preview: bool = False
    keep_going: bool = False
    case_timeout: float = 120
    profile: str | None = None
    sampling: str | None = None
    baseline: Path | None = None
    max_regression: float = 0.1
    worker: bool = False
    directory: Path | None = None
    port: int = 0
    sink_port: int = 0
    vis_pixels: int = 81


def parse_args(argv: list[str] | None = None) -> Options:
    p = argparse.ArgumentParser(description="Measure a selected LedFx checkout")
    p.add_argument("--repo", type=Path, required=True, help="LedFx source checkout")
    p.add_argument(
        "--python",
        type=Path,
        required=True,
        help="Interpreter/environment used for the LedFx worker",
    )
    p.add_argument("--output", type=Path, default=Path("artifacts/pixels.jsonl"))
    p.add_argument("--receiver", type=Path, help="External native DDP receiver binary")
    p.add_argument("--pixels", type=int, default=50000)
    p.add_argument("--rows", type=int, default=0)
    p.add_argument("--effects", default="rainbow", help="Comma-separated IDs or all")
    p.add_argument("--effect-config", default="{}", help="JSON configuration overrides")
    p.add_argument("--streams", default="none,full")
    p.add_argument("--seconds", type=float, default=8)
    p.add_argument("--warmup", type=float, default=2)
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--bind", default="127.0.0.1", help="IPv4 loopback address")
    p.add_argument("--device", choices=("ddp", "dummy"), default="ddp")
    p.add_argument("--refresh-rate", type=int, default=60)
    p.add_argument("--preview-fps", type=int, default=60)
    p.add_argument("--effect-speed", type=float, default=6)
    p.add_argument("--seed", type=int, default=2058)
    for flag in (
        "synthetic-audio",
        "fixtures",
        "unpaced",
        "unpaced-preview",
        "keep-going",
    ):
        p.add_argument("--" + flag, action="store_true")
    p.add_argument("--case-timeout", type=float, default=120)
    p.add_argument("--profile", choices=("main", "render", "effect"))
    p.add_argument(
        "--sampling",
        choices=("wall", "cpu", "gil"),
        help="Python 3.15+ sampling in the selected worker interpreter",
    )
    p.add_argument("--baseline", type=Path)
    p.add_argument("--max-regression", type=float, default=0.1)
    p.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--effect", default="rainbow", help=argparse.SUPPRESS)
    p.add_argument("--directory", type=Path, help=argparse.SUPPRESS)
    p.add_argument("--port", type=int, default=0, help=argparse.SUPPRESS)
    p.add_argument("--sink-port", type=int, default=0, help=argparse.SUPPRESS)
    p.add_argument("--vis-pixels", type=int, default=81, help=argparse.SUPPRESS)
    args = p.parse_args(argv, namespace=Options())
    try:
        args.repo = checkout(args.repo)
        args.python = args.python.absolute()
        if not args.python.is_file():
            raise ValueError("--python must name an existing interpreter")
        args.output = artifact_path(args.output)
        if args.output.is_relative_to(args.repo):
            raise ValueError("Results must live outside the selected LedFx checkout")
        if args.receiver is not None:
            args.receiver = args.receiver.resolve()
            if not args.receiver.is_file():
                raise ValueError("--receiver must name an existing executable")
        address = ipaddress.ip_address(args.bind)
        if address.version != 4 or not address.is_loopback:
            raise ValueError("--bind must be an IPv4 loopback address")
        decode_record(args.effect_config)
        if args.rows < 0 or (args.rows and args.pixels % args.rows):
            raise ValueError("--rows must divide --pixels exactly")
        if args.pixels <= 0 or args.vis_pixels <= 0 or args.repeats <= 0:
            raise ValueError("Pixel counts and repeats must be positive")
        if (
            not all(
                math.isfinite(n)
                for n in (
                    args.seconds,
                    args.warmup,
                    args.case_timeout,
                    args.max_regression,
                )
            )
            or args.seconds <= 0
            or args.warmup < 0
            or args.case_timeout <= 0
        ):
            raise ValueError("Invalid measurement duration/deadline")
        if not 0 <= args.seed < 2**32 or not 0 <= args.max_regression < 1:
            raise ValueError("Invalid seed or regression threshold")
        if args.refresh_rate <= 0 or not 1 <= args.preview_fps <= 60:
            raise ValueError("Refresh must be positive; preview FPS must be 1–60")
        if not 0.1 <= args.effect_speed <= 20:
            raise ValueError("Rainbow effect speed must be 0.1–20")
        if not all(s in {"none", "default", "full"} for s in args.streams.split(",")):
            raise ValueError("Streams must contain none,default,full")
        if len(set(args.effects.split(","))) != len(args.effects.split(",")):
            raise ValueError("Duplicate effects would overwrite per-case diagnostics")
        if len(set(args.streams.split(","))) != len(args.streams.split(",")):
            raise ValueError("Duplicate streams would overwrite per-case diagnostics")
        if not args.effects or any(not s for s in args.effects.split(",")):
            raise ValueError("Effects must be nonempty")
        if any(not name.isidentifier() for name in args.effects.split(",")):
            raise ValueError("Effects must be registered effect identifiers")
        if not args.effect.isidentifier():
            raise ValueError("Worker effect must be a registered identifier")
        if args.profile and args.sampling:
            raise ValueError("Sampling and cProfile must be separate runs")
        if args.worker and (not args.directory or not args.port or not args.sink_port):
            raise ValueError("Worker requires directory, port and sink-port")
    except ValueError as error:
        p.error(str(error))
    return args
