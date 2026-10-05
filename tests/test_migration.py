"""Checkout/interpreter selection and failure ownership after repository split."""

import json
import subprocess
import sys
from pathlib import Path
from typing import cast

import psutil
import pytest

from ledfx_performance import matrix
from ledfx_performance.data import Record, artifact_path, command, decode_record
from ledfx_performance.options import parse_args
from ledfx_performance.process import run_retained_process


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "selected-app"
    app = root / "ledfx"
    effects = app / "effects"
    effects.mkdir(parents=True)
    (app / "__init__.py").write_text("")
    (app / "core.py").write_text("# Test source marker\n")
    (effects / "__init__.py").write_text(
        "class Selected:\n NAME='selected checkout'; CATEGORY='Other'\n"
        "class Effects:\n def __init__(self,owner): pass\n"
        " def classes(self): return {'selected':Selected}\n"
    )
    for module, cls in (
        ("audio", "AudioReactiveEffect"),
        ("temporal", "TemporalEffect"),
        ("twod", "Twod"),
    ):
        (effects / f"{module}.py").write_text(f"class {cls}: pass\n")
    old_tools = root / "tools"
    old_tools.mkdir()
    (old_tools / "__init__.py").write_text("raise RuntimeError('app tools imported')\n")
    return root


def test_launcher_selects_app_from_other_cwd_without_importing_app_tools(
    repo: Path, tmp_path: Path
) -> None:
    foreign = tmp_path / "foreign"
    (foreign / "ledfx_performance").mkdir(parents=True)
    (foreign / "ledfx_performance/__init__.py").write_text(
        "raise RuntimeError('wrong package')\n"
    )
    argv = command(
        Path(sys.executable), "ledfx_performance.fixtures", ["--repo", str(repo)]
    )
    assert argv[0] == str(Path(sys.executable).absolute())
    result = subprocess.run(
        argv, cwd=foreign, text=True, capture_output=True, check=True
    )
    assert decode_record(result.stdout)["selected"] == {
        "name": "selected checkout",
        "matrix": False,
        "audio": False,
        "temporal": False,
    }


@pytest.mark.parametrize(
    "options",
    [
        ["--rows", "3", "--pixels", "10"],
        ["--effect-config", "[]"],
        ["--case-timeout", "nan"],
        ["--bind", "192.0.2.1"],
        ["--sampling", "cpu", "--profile", "main"],
    ],
)
def test_invalid_workload_arguments_fail_early(
    repo: Path, tmp_path: Path, options: list[str]
) -> None:
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--repo",
                str(repo),
                "--python",
                sys.executable,
                "--output",
                str(tmp_path / "results.jsonl"),
                *options,
            ]
        )


def test_results_cannot_be_written_into_tracked_tools_source() -> None:
    root = Path(__file__).resolve().parents[1]
    with pytest.raises(ValueError, match="artifacts"):
        artifact_path(root / "results.jsonl")
    assert artifact_path(root / "artifacts/test/results.jsonl").is_relative_to(
        root / "artifacts"
    )


def test_results_cannot_modify_selected_app(repo: Path) -> None:
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--repo",
                str(repo),
                "--python",
                sys.executable,
                "--output",
                str(repo / "results.jsonl"),
            ]
        )


def test_timeout_retains_partial_streams_and_reaps_descendants(tmp_path: Path) -> None:
    script = tmp_path / "hang.py"
    script.write_text(
        "import subprocess,sys\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'])\n"
        "print('child='+str(child.pid),flush=True)\n"
        "print('partial stderr',file=sys.stderr,flush=True)\nchild.wait()\n"
    )
    prefix = tmp_path / "trial"
    result = run_retained_process([sys.executable, str(script)], prefix, timeout=1)
    assert result["timed_out"] is True and result["status"] == "partial"
    assert "partial stderr" in prefix.with_suffix(".stderr").read_text()
    child_pid = int(
        prefix.with_suffix(".stdout").read_text().split("child=")[1].splitlines()[0]
    )
    assert not psutil.pid_exists(child_pid)
    assert json.loads(prefix.with_suffix(".process.json").read_text()) == result


def test_matrix_pairs_explicit_apps_and_interpreters(
    repo: Path, tmp_path: Path
) -> None:
    args = matrix.Settings(
        baseline_repo=repo,
        candidate_repo=tmp_path / "candidate",
        baseline_python=Path("/baseline/python"),
        candidate_python=Path("/candidate/python"),
        output=tmp_path / "trial",
        effects="rainbow",
        streams="full",
        repeats=3,
    )
    rows = matrix.trials(args)
    assert len(rows) == 6
    for a, b in zip(rows[::2], rows[1::2], strict=True):
        assert {a["label"], b["label"]} == {"baseline", "candidate"}
        assert a["repeat"] == b["repeat"]
    for row in rows:
        argv = cast(list[str], row["command"])
        label = str(row["label"])
        assert argv[0] == str(Path(sys.executable).absolute())
        assert argv[argv.index("--python") + 1] == f"/{label}/python"
        assert "tools/pixel_bench.py" not in " ".join(argv)
        assert argv[argv.index("--repo") + 1] == str(
            repo if label == "baseline" else tmp_path / "candidate"
        )


def test_matrix_persists_partial_outcome_before_aborting(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "campaign"
    argv = [
        "matrix",
        "--baseline-repo",
        str(repo),
        "--candidate-repo",
        str(repo),
        "--baseline-python",
        sys.executable,
        "--candidate-python",
        sys.executable,
        "--output",
        str(output),
        "--effects",
        "rainbow",
        "--streams",
        "none",
        "--repeats",
        "1",
    ]
    monkeypatch.setattr(sys, "argv", argv)

    def fake_revision(repo: Path) -> str:
        return "test-revision"

    monkeypatch.setattr(matrix, "revision", fake_revision)

    def fail(command: list[str], prefix: Path, *, timeout: float, cwd: Path) -> Record:
        stderr = prefix.with_suffix(".stderr")
        stderr.write_text("partial worker diagnostic")
        return {
            "status": "partial",
            "timed_out": True,
            "returncode": -9,
            "stderr": str(stderr),
        }

    monkeypatch.setattr(matrix, "run_retained_process", fail)
    with pytest.raises(RuntimeError, match="retained"):
        matrix.main()
    outcomes = [
        decode_record(line)
        for line in (output / "outcomes.jsonl").read_text().splitlines()
    ]
    assert len(outcomes) == 1 and outcomes[0]["status"] == "partial"
    assert json.loads((output / "summary.json").read_text())["complete_pairs"] == 0


def test_failed_output_reservation_and_sidecars_are_never_overwritten(
    tmp_path: Path,
) -> None:
    from ledfx_performance.pixel import reserve_output

    output = tmp_path / "failed.jsonl"
    reserve_output(output)
    assert output.is_file() and output.read_text() == ""
    diagnostic = Path(str(output) + ".failures.jsonl")
    diagnostic.write_text("partial diagnostic")
    with pytest.raises(FileExistsError):
        reserve_output(output)
    output.unlink()
    with pytest.raises(FileExistsError):
        reserve_output(output)
    assert diagnostic.read_text() == "partial diagnostic"


def test_source_manifest_reads_only_selected_python_tree(tmp_path: Path) -> None:
    from ledfx_performance.data import source_manifest

    package = tmp_path / "src/package"
    package.mkdir(parents=True)
    (package / "test.py").write_text("value = 1\n")
    unrelated = tmp_path / "discord"
    unrelated.mkdir()
    (unrelated / ".env").write_text("test sentinel, not a credential\n")
    assert list(source_manifest(tmp_path, package)) == ["src/package/test.py"]


def test_receiver_uses_its_snapshot_interval(tmp_path: Path) -> None:
    import socket
    import struct
    import time

    from ledfx_performance.receiver import DDPCollector

    collector = DDPCollector(1, "127.0.0.1")
    try:
        before = collector.snapshot()
        payload = struct.pack(">BBBBIH", 0x41, 1, 0x0B, 1, 0, 3) + b"rgb"
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(payload, ("127.0.0.1", collector.port))
        time.sleep(0.02)
        after = collector.snapshot()
        interval = collector.interval()
        assert after[3] == before[3] + 1
        assert isinstance(interval["seconds"], float) and interval["seconds"] > 0
        assert interval["snapshots"] == collector.snapshots[-2:]
    finally:
        collector.close()
