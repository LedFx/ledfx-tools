"""Checked JSON boundaries, source selection, and artifact ownership."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path
from typing import TypeAlias, cast

Json: TypeAlias = None | bool | int | float | str | list["Json"] | dict[str, "Json"]
Record: TypeAlias = dict[str, Json]


def read_record(path: Path) -> Record:
    return decode_record(path.read_text(encoding="utf-8"))


def decode_record(text: str) -> Record:
    value: object = json.loads(text)
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise ValueError("Expected a JSON object")
    return cast(Record, value)


def number(record: Record, key: str) -> float:
    value = record[key]
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{key} must be numeric")
    return float(value)


def integer(record: Record, key: str) -> int:
    value = record[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return value


def text(record: Record, key: str) -> str:
    value = record[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    return value


def write_record(path: Path, record: Record) -> None:
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def checkout(repo: Path) -> Path:
    repo = repo.resolve()
    if not (repo / "ledfx/core.py").is_file():
        raise ValueError(f"Not a LedFx source checkout: {repo}")
    return repo


def select_checkout(repo: Path) -> None:
    """Select app source without importing its tools or allowing a stale app."""
    repo = checkout(repo)
    if "ledfx" in sys.modules:
        raise RuntimeError("Select the LedFx checkout before importing LedFx")
    sys.path.insert(0, str(repo))


def command(python: Path, module: str, arguments: list[str]) -> list[str]:
    """Use a package launcher, independent of worker cwd and installed tools."""
    launcher = Path(__file__).resolve().with_name("entry.py")
    return [str(python.absolute()), str(launcher), module, *arguments]


def revision(repo: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()


def artifact_path(path: Path) -> Path:
    path = path.resolve()
    source = Path(__file__).resolve().parents[2]
    if path.is_relative_to(source) and not path.is_relative_to(source / "artifacts"):
        raise ValueError("Use artifacts/ or a path outside the tools checkout")
    return path


def source_manifest(root: Path, source: Path) -> Record:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(source.rglob("*.py"))
    }


def working_tree(repo: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), "status", "--short"],
        text=True,
    ).strip()


def sender_manifest() -> Record:
    """Identify installed implementation bytes, including same-version local wheels."""
    try:
        distribution = importlib.metadata.distribution("ledfx-senders")
    except importlib.metadata.PackageNotFoundError:
        return {}
    return {
        str(file): hashlib.sha256(
            Path(str(distribution.locate_file(file))).read_bytes()
        ).hexdigest()
        for file in distribution.files or []
        if str(file).startswith("ledfx_senders/")
        and "__pycache__/" not in str(file)
        and file.suffix != ".pyc"
    }
