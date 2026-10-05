"""Rank active effect/audio/preview stacks, excluding imports and shutdown.

Replay sampling with the same interpreter version used to capture it. Warmup
remains in sampler recordings; short catalog profiles are screening evidence.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import cast

from ledfx_performance.data import (
    Record,
    decode_record,
    number,
    text,
    write_record,
)

ACTIVE_STACKS = (
    "Virtual.thread_function",
    "TemporalEffect.thread_function",
    "_audio_sample_callback",
    "handle_visualisation_update",
    "WebsocketConnection",
)


def summarize_stacks(
    lines: list[str],
    active_stacks: tuple[str, ...] = ACTIVE_STACKS,
) -> Record:
    total = active = 0
    leaf: collections.Counter[str] = collections.Counter()
    inclusive: collections.Counter[str] = collections.Counter()
    for line in lines:
        if not line.strip():
            continue
        stack, count_text = line.rsplit(" ", 1)
        count = int(count_text)
        total += count
        if not any(marker in stack for marker in active_stacks):
            continue
        frames = [
            frame.rsplit(":", 1)[0]
            for frame in stack.split(";")
            if not frame.startswith("tid:")
        ]
        if not frames:
            continue
        active += count
        leaf[frames[-1]] += count
        inclusive.update({frame: count for frame in set(frames)})

    def top(counter: collections.Counter[str]) -> list[Record]:
        return (
            [
                {
                    "frame": name,
                    "samples": n,
                    "active_sample_pct": round(n / active * 100, 2),
                }
                for name, n in counter.most_common(15)
            ]
            if active
            else []
        )

    return {
        "all_samples": total,
        "active_samples": active,
        "top_self": list(top(leaf)),
        "top_inclusive": list(top(inclusive)),
    }


def report(results: Path, python: Path, timeout: float = 120) -> list[Record]:
    rows = [decode_record(line) for line in results.read_text().splitlines()]
    summaries: list[Record] = []
    for row in rows:
        effect = text(row, "effect_id")
        folder = (
            Path(str(results) + ".artifacts")
            / effect
            / f"{row['repeat']}-{row['stream']}"
        )
        binary = folder / "sampling.bin"
        profile: Record = {}
        if binary.exists():
            collapsed = folder / "sampling.collapsed"
            with (folder / "replay.log").open("w") as log:
                subprocess.run(
                    [
                        str(python),
                        "-m",
                        "profiling.sampling",
                        "replay",
                        "--collapsed",
                        "-o",
                        str(collapsed),
                        str(binary),
                    ],
                    stdout=log,
                    stderr=log,
                    check=True,
                    timeout=timeout,
                )
            profile = summarize_stacks(collapsed.read_text().splitlines())
        summaries.append(
            {
                "effect": effect,
                "rows": row["rows"],
                "stream": row["stream"],
                "profiled": row["profiled"],
                "unpaced": row["unpaced"],
                "render_fps": number(cast(Record, row["assemble"]), "fps"),
                "effect_ms": number(cast(Record, row["effect"]), "mean_ms"),
                "audio_update_ms": number(cast(Record, row["audio_update"]), "mean_ms"),
                "server_cpu_pct": row["server_cpu_pct"],
                "rss_mb": row["rss_mb"],
                "ws_changed_fps": row["ws_changed_fps"],
                "ddp_complete_fps": row["ddp_complete_fps"],
                "profile": profile,
            }
        )
    summaries.sort(
        key=lambda row: number(row, "effect_ms") + number(row, "audio_update_ms"),
        reverse=True,
    )
    return summaries


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument(
        "--timeout", type=float, default=120, help="Per-recording replay deadline"
    )
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("Replay timeout must be finite and positive")
    summaries = report(Path(args.results), Path(args.python), args.timeout)
    destination = Path(str(args.results) + ".profiles.json")
    write_record(destination, {"effects": list(summaries)})
    print(destination)
    for row in summaries:
        print(json.dumps(row))


if __name__ == "__main__":
    main()
