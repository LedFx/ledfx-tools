"""Launch a package command with an arbitrary supplied worker interpreter."""

import runpy
import sys
from pathlib import Path


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    module = sys.argv.pop(1)
    if not module.startswith("ledfx_performance."):
        raise ValueError("Only performance package commands may be launched")
    runpy.run_module(module, run_name="__main__")


if __name__ == "__main__":
    main()
