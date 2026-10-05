"""Migrated output and fixture tests reject plausible but incomplete throughput."""

import base64
import struct
from typing import cast

import numpy as np
import pytest

from ledfx_performance.data import Record
from ledfx_performance.fixtures import audio_signal, matrix_rows
from ledfx_performance.pixel import check_regression, scenario_key, validate_frame
from ledfx_performance.profiles import summarize_stacks
from ledfx_performance.receiver import DDPReceiver


def packet(
    offset: int, payload: bytes, *, push: bool = False, sequence: int = 1
) -> bytes:
    return (
        struct.pack(">BBBBIH", 0x40 | push, sequence, 0x0B, 1, offset, len(payload))
        + payload
    )


def test_ddp_requires_ordered_complete_matching_sequence() -> None:
    receiver = DDPReceiver(4)
    receiver.feed(packet(0, b"abcdef"))
    receiver.feed(packet(6, b"ghijkl", push=True))
    assert receiver.counts[2:] == [1, 1, 0]
    receiver.feed(packet(0, b"abc", sequence=2))
    receiver.feed(packet(9, b"jkl", push=True, sequence=2))
    assert receiver.counts[2:] == [2, 1, 0]
    receiver.feed(packet(0, b"abcdef", sequence=3))
    receiver.feed(packet(6, b"ghijkl", push=True, sequence=4))
    assert receiver.counts[3] == 1
    receiver.feed(packet(0, b"abcdefghijkl", push=True, sequence=5))
    assert receiver.counts[3] == 2


@pytest.mark.parametrize(
    "data", [b"short", packet(0, b"abcdef")[:-1], packet(9, b"abcdef")]
)
def test_ddp_rejects_malformed_packets(data: bytes) -> None:
    receiver = DDPReceiver(4)
    receiver.feed(data)
    assert receiver.counts[4] == 1


def test_preview_validates_shape_base64_and_complete_rgb() -> None:
    encoded = base64.b64encode(b"abcdef").decode("ascii")
    assert validate_frame({"shape": [1, 2], "pixels": encoded}, 2) == [1, 2]
    for event in (
        {"shape": [1, 2], "pixels": encoded[:-4]},
        {"shape": [1, 1], "pixels": encoded},
        {"shape": [True, 2], "pixels": encoded},
        {"shape": [1, 2], "pixels": "!!!!!!!!"},
    ):
        with pytest.raises(ValueError):
            validate_frame(cast(Record, event), 2)


def test_synthetic_pcm_is_repeatable_bounded_and_pulsed() -> None:
    signal = audio_signal()
    np.testing.assert_array_equal(signal, audio_signal())
    assert signal.dtype == np.float32
    assert len(signal) == 30000 * 8
    assert np.isfinite(signal).all()
    assert np.max(np.abs(signal)) <= 1
    beats = signal.reshape(-1, 15000)
    assert np.mean(beats[:, :1500] ** 2) > np.mean(beats[:, 12000:] ** 2)


@pytest.mark.parametrize("pixels,rows", [(500000, 625), (1024, 32), (81, 9), (17, 1)])
def test_matrix_layout_preserves_every_pixel(pixels: int, rows: int) -> None:
    assert matrix_rows(pixels) == rows
    assert pixels % rows == 0


def test_profiles_exclude_startup_and_count_recursive_frames_once() -> None:
    report = summarize_stacks(
        [
            "tid:1;importlib.py:import_module:1 100",
            "tid:2;virtuals.py:Virtual.thread_function:1;"
            "effect.py:render:1;effect.py:render:2 20",
            "tid:3;audio.py:_audio_sample_callback:1;audio.py:fft:2 10",
        ]
    )
    assert report["all_samples"] == 130
    assert report["active_samples"] == 30
    top = cast(list[Record], report["top_self"])
    assert top[0]["frame"] == "effect.py:render" and top[0]["samples"] == 20
    inclusive = cast(list[Record], report["top_inclusive"])
    assert (
        next(r for r in inclusive if r["frame"] == "effect.py:render")["samples"] == 20
    )


def test_baseline_uses_medians_and_refuses_different_workloads() -> None:
    rows: list[Record] = [
        {
            "platform": "linux",
            "python": "3.12",
            "loop": "standard",
            "device": "ddp",
            "pixels": 50000,
            "stream": "full",
            "assemble": {"fps": fps},
            "ws_fps": fps,
            "ddp_complete_fps": fps,
        }
        for fps in (60.0, 60.0, 1.0)
    ]
    changed = [{**rows[0], "ws_fps": 40.0}]
    assert len(check_regression(rows, changed, 0.1)) == 1
    assert check_regression(rows, rows, 0.1) == []
    for flag in (
        "unpaced",
        "unpaced_preview",
        "profiled",
        "synthetic_audio",
        "fixtures",
    ):
        assert (
            "No matching baseline"
            in check_regression(rows, [{**rows[0], flag: True}], 0.1)[0]
        )
    assert scenario_key(rows[0] | {"effect_config": {"a": 1, "b": 2}}) == scenario_key(
        rows[0] | {"effect_config": {"b": 2, "a": 1}}
    )


def test_generated_media_paths_match_only_when_content_and_effect_match() -> None:
    before: Record = {
        "effect_id": "gifplayer",
        "fixtures": True,
        "effect_config": {"image_location": "/before/fixture.gif", "speed": 1},
        "fixture_files": {
            "fixture.gif": {"path": "/before/fixture.gif", "sha256": "same"}
        },
    }
    after: Record = {
        **before,
        "effect_config": {"image_location": "/after/fixture.gif", "speed": 1},
        "fixture_files": {
            "fixture.gif": {"path": "/after/fixture.gif", "sha256": "same"}
        },
    }
    assert scenario_key(before) == scenario_key(after)
    assert scenario_key(before) != scenario_key(
        after
        | {
            "fixture_files": {
                "fixture.gif": {"path": "/after/fixture.gif", "sha256": "different"}
            },
        }
    )
    assert scenario_key(before) != scenario_key(
        after
        | {
            "effect_config": {"image_location": "/after/fixture.gif", "speed": 2},
        }
    )
    assert scenario_key(before | {"receiver_identity": "a"}) != scenario_key(
        before | {"receiver_identity": "b"}
    )
