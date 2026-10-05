"""Randomized adjacent before/after app comparisons with retained failed trials."""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from ledfx_performance.data import (
    Json,
    Record,
    artifact_path,
    checkout,
    command,
    decode_record,
    revision,
    text,
    write_record,
)
from ledfx_performance.pixel import scenario_key, throughput
from ledfx_performance.process import run_retained_process


@dataclass
class Settings:
    baseline_repo: Path = field(default_factory=Path)
    candidate_repo: Path = field(default_factory=Path)
    baseline_python: Path = field(default_factory=lambda: Path(sys.executable))
    candidate_python: Path = field(default_factory=lambda: Path(sys.executable))
    output: Path = field(default_factory=lambda: Path("artifacts/paired"))
    receiver: Path | None = None
    effects: str = "singleColor,rainbow,smoke2d"
    streams: str = "none,full"
    pixels: int = 50000
    rows: int = 0
    seconds: float = 8
    warmup: float = 2
    repeats: int = 3
    seed: int = 2058
    bind: str = "127.0.0.1"
    unpaced: bool = False
    unpaced_preview: bool = False
    plan_only: bool = False
    keep_going: bool = False
    timeout: float = 120
    effect_config: str = "{}"
    fixtures: bool = False


def parse_args(argv: list[str] | None = None) -> Settings:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in (
        "baseline-repo",
        "candidate-repo",
        "baseline-python",
        "candidate-python",
    ):
        parser.add_argument("--" + flag, type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--receiver", type=Path)
    parser.add_argument("--effects", default="singleColor,rainbow,smoke2d")
    parser.add_argument("--streams", default="none,full")
    parser.add_argument("--effect-config", default="{}")
    parser.add_argument("--pixels", type=int, default=50000)
    parser.add_argument("--rows", type=int, default=0)
    parser.add_argument("--seconds", type=float, default=8)
    parser.add_argument("--warmup", type=float, default=2)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=2058)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--timeout", type=float, default=120)
    for flag in ("plan-only", "keep-going", "unpaced", "unpaced-preview", "fixtures"):
        parser.add_argument("--" + flag, action="store_true")
    args = parser.parse_args(argv, namespace=Settings())
    try:
        args.baseline_repo, args.candidate_repo = (
            checkout(args.baseline_repo),
            checkout(args.candidate_repo),
        )
        args.baseline_python = args.baseline_python.absolute()
        args.candidate_python = args.candidate_python.absolute()
        if not args.baseline_python.is_file() or not args.candidate_python.is_file():
            raise ValueError("Supply existing interpreters for both apps")
        args.output = artifact_path(args.output)
        if any(
            args.output.is_relative_to(repo)
            for repo in (
                args.baseline_repo,
                args.candidate_repo,
            )
        ):
            raise ValueError("Results must live outside both LedFx checkouts")
        decode_record(args.effect_config)
        if (
            args.pixels <= 0
            or args.repeats <= 0
            or args.rows < 0
            or (args.rows and args.pixels % args.rows)
        ):
            raise ValueError("Invalid pixel geometry or repeat count")
        if (
            not all(math.isfinite(n) for n in (args.seconds, args.warmup, args.timeout))
            or args.seconds <= 0
            or args.warmup < 0
            or args.timeout <= 0
        ):
            raise ValueError("Invalid duration/deadline")
        if len(set(args.effects.split(","))) != len(args.effects.split(",")):
            raise ValueError("Duplicate effects would overwrite trial files")
        if len(set(args.streams.split(","))) != len(args.streams.split(",")):
            raise ValueError("Duplicate streams would overwrite trial files")
        if not args.effects or any(not name for name in args.effects.split(",")):
            raise ValueError("Effects must be nonempty")
        if any(not name.isidentifier() for name in args.effects.split(",")):
            raise ValueError("Effects must be registered effect identifiers")
        if not all(
            name in ("none", "default", "full") for name in args.streams.split(",")
        ):
            raise ValueError("Unknown preview stream")
        if args.receiver is not None:
            args.receiver = args.receiver.resolve()
            if not args.receiver.is_file():
                raise ValueError("Receiver must exist")
    except ValueError as error:
        parser.error(str(error))
    return args


def trials(args: Settings) -> list[Record]:
    rng = random.Random(args.seed)
    result: list[Record] = []
    for repeat in range(args.repeats):
        cases = [
            (effect, stream)
            for effect in args.effects.split(",")
            for stream in args.streams.split(",")
        ]
        rng.shuffle(cases)
        for effect, stream in cases:
            labels = ["baseline", "candidate"]
            rng.shuffle(labels)
            for label in labels:
                repo = (
                    args.baseline_repo if label == "baseline" else args.candidate_repo
                )
                python = (
                    args.baseline_python
                    if label == "baseline"
                    else args.candidate_python
                )
                key = f"{repeat}-{effect}-{stream}-{label}"
                row_path = args.output / f"{key}.jsonl"
                arguments = [
                    "--repo",
                    str(repo),
                    "--python",
                    str(python),
                    "--output",
                    str(row_path),
                    "--pixels",
                    str(args.pixels),
                    "--rows",
                    str(args.rows),
                    "--effects",
                    effect,
                    "--streams",
                    stream,
                    "--seconds",
                    str(args.seconds),
                    "--warmup",
                    str(args.warmup),
                    "--repeats",
                    "1",
                    "--seed",
                    str(args.seed),
                    "--synthetic-audio",
                    "--bind",
                    args.bind,
                    "--effect-config",
                    args.effect_config,
                    "--case-timeout",
                    str(args.timeout),
                ]
                if args.receiver:
                    arguments.extend(["--receiver", str(args.receiver)])
                for name in ("unpaced", "unpaced_preview", "fixtures"):
                    if getattr(args, name):
                        arguments.append("--" + name.replace("_", "-"))
                result.append(
                    {
                        "key": key,
                        "label": label,
                        "repeat": repeat,
                        "effect": effect,
                        "stream": stream,
                        "result": str(row_path),
                        "command": [
                            cast(Json, item)
                            for item in command(
                                Path(sys.executable),
                                "ledfx_performance.pixel",
                                arguments,
                            )
                        ],
                    }
                )
    return result


def paired_summary(outcomes: list[Record]) -> Record:
    pairs: dict[str, dict[str, Record]] = {}
    for outcome in outcomes:
        if outcome["status"] != "ok":
            continue
        rows = Path(text(outcome, "result")).read_text().splitlines()
        if len(rows) != 1:
            raise ValueError("Each paired trial must contain one result")
        key = f"{outcome['repeat']}-{outcome['effect']}-{outcome['stream']}"
        pairs.setdefault(key, {})[text(outcome, "label")] = decode_record(rows[0])
    ratios: dict[str, list[float]] = {}
    complete = 0
    for key, pair in pairs.items():
        if set(pair) != {"baseline", "candidate"}:
            continue
        if scenario_key(pair["baseline"] | {"python": None}) != scenario_key(
            pair["candidate"] | {"python": None}
        ):
            raise ValueError(f"Paired workloads differ: {key}")
        complete += 1
        before, after = throughput(pair["baseline"]), throughput(pair["candidate"])
        for metric, value in before.items():
            if value > 0:
                scenario = key.split("-", 1)[1] + "-" + metric
                ratios.setdefault(scenario, []).append(after[metric] / value)
    return {
        "complete_pairs": complete,
        "failed_trials": sum(row["status"] != "ok" for row in outcomes),
        "ratios": {
            key: {
                "n": len(values),
                "median": statistics.median(values),
                "min": min(values),
                "max": max(values),
            }
            for key, values in ratios.items()
        },
        "scope": (
            "Whole-app/environment change; not sender-only attribution. "
            "Short trials screen throughput, not hardware latency or peak memory."
        ),
    }


def main() -> None:
    args = parse_args()
    if args.output.exists():
        raise SystemExit(
            "Use a new output directory; existing trials are never overwritten"
        )
    args.output.mkdir(parents=True)
    planned = trials(args)
    plan: Record = {
        "baseline_revision": revision(args.baseline_repo),
        "candidate_revision": revision(args.candidate_repo),
        "baseline_repo": str(args.baseline_repo),
        "candidate_repo": str(args.candidate_repo),
        "baseline_python": str(args.baseline_python),
        "candidate_python": str(args.candidate_python),
        "seed": args.seed,
        "trials": list(planned),
        "order": "Randomized adjacent treatments within shuffled repeat blocks",
    }
    write_record(args.output / "manifest.json", plan)
    if args.plan_only:
        print(f"Planned {len(planned)} trials in {args.output}")
        return
    outcomes: list[Record] = []
    for trial in planned:
        argv = trial["command"]
        if not isinstance(argv, list) or not all(
            isinstance(item, str) for item in argv
        ):
            raise ValueError("Malformed trial command")
        process = run_retained_process(
            cast(list[str], argv),
            args.output / text(trial, "key"),
            timeout=args.timeout + 15,
            cwd=args.output,
        )
        outcome = {**trial, "process": process, "status": process["status"]}
        outcomes.append(outcome)
        with (args.output / "outcomes.jsonl").open("a") as output:
            output.write(json.dumps(outcome) + "\n")
        write_record(args.output / "summary.json", paired_summary(outcomes))
        print(f"{outcome['status']}: {trial['key']}", flush=True)
        if process["status"] != "ok" and not args.keep_going:
            raise RuntimeError("Failed trial; retained partial output and outcome")
    if any(row["status"] != "ok" for row in outcomes):
        raise SystemExit("Some paired trials failed; see outcomes.jsonl")


if __name__ == "__main__":
    main()
