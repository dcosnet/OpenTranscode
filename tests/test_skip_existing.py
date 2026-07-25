"""
v4.3.0: skip-existing detection tests.

When the output file already exists with a matching video+audio codec,
the file is skipped instead of re-encoded. This is the default
(--skip-existing); pass --force-reencode to disable.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from conftest import make_minimal_worker


def _ffprobe_result(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["ffprobe"], returncode=0,
        stdout=json.dumps(payload), stderr="",
    )


def _av1_opus_output() -> dict:
    """ffprobe JSON for an AV1+Opus file (matches VIDEO_CODECS[0] + AUDIO_PROFILES[0])."""
    return {
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "av1",
             "width": 1920, "height": 1080},
            {"index": 1, "codec_type": "audio", "codec_name": "opus"},
        ],
        "format": {"duration": "10.0"},
    }


def _h264_aac_output() -> dict:
    """ffprobe JSON for an H.264+AAC file (does NOT match AV1+Opus profile)."""
    return {
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264",
             "width": 1920, "height": 1080},
            {"index": 1, "codec_type": "audio", "codec_name": "aac"},
        ],
        "format": {"duration": "10.0"},
    }


def test_skip_existing_returns_false_when_output_missing(opentranscode_module, mock_env, tmp_path):
    """No output file → don't skip (proceed with encode)."""
    worker = make_minimal_worker(opentranscode_module, env=mock_env)
    worker.skip_existing = True
    # Set up the codec profile so ffprobe_codec_name is populated.
    worker.video_codec = opentranscode_module.VIDEO_CODECS[0]  # AV1
    worker.audio_profile = opentranscode_module.AUDIO_PROFILES[0]  # Opus
    worker.resolution = opentranscode_module.RESOLUTION_PRESETS[0]  # Original

    output_f = tmp_path / "nonexistent_archived.mkv"
    assert not worker._output_already_encoded(tmp_path / "source.mkv", output_f)


def test_skip_existing_returns_true_when_codec_matches(opentranscode_module, mock_env, tmp_path, monkeypatch):
    """Output exists + ffprobe reads it + codec matches → skip."""
    worker = make_minimal_worker(opentranscode_module, env=mock_env)
    worker.skip_existing = True
    worker.video_codec = opentranscode_module.VIDEO_CODECS[0]  # AV1 → ffprobe_codec_name="av1"
    worker.audio_profile = opentranscode_module.AUDIO_PROFILES[0]  # Opus → "opus"
    worker.resolution = opentranscode_module.RESOLUTION_PRESETS[0]  # Original (no scaling)

    output_f = tmp_path / "output_archived.mkv"
    output_f.write_bytes(b"fake mkv content")

    monkeypatch.setattr(
        "subprocess.run",
        MagicMock(return_value=_ffprobe_result(_av1_opus_output())),
    )

    assert worker._output_already_encoded(tmp_path / "source.mkv", output_f) is True


def test_skip_existing_returns_false_when_video_codec_mismatches(opentranscode_module, mock_env, tmp_path, monkeypatch):
    """Output exists but video codec is h264 (not av1) → don't skip."""
    worker = make_minimal_worker(opentranscode_module, env=mock_env)
    worker.skip_existing = True
    worker.video_codec = opentranscode_module.VIDEO_CODECS[0]  # AV1
    worker.audio_profile = opentranscode_module.AUDIO_PROFILES[0]  # Opus
    worker.resolution = opentranscode_module.RESOLUTION_PRESETS[0]

    output_f = tmp_path / "output_archived.mkv"
    output_f.write_bytes(b"fake mkv content")

    # ffprobe says h264/aac, but we selected AV1/Opus → mismatch → don't skip.
    monkeypatch.setattr(
        "subprocess.run",
        MagicMock(return_value=_ffprobe_result(_h264_aac_output())),
    )

    assert worker._output_already_encoded(tmp_path / "source.mkv", output_f) is False


def test_skip_existing_returns_false_when_ffprobe_fails(opentranscode_module, mock_env, tmp_path, monkeypatch):
    """Output exists but ffprobe returns None (corrupt) → don't skip."""
    worker = make_minimal_worker(opentranscode_module, env=mock_env)
    worker.skip_existing = True
    worker.video_codec = opentranscode_module.VIDEO_CODECS[0]
    worker.audio_profile = opentranscode_module.AUDIO_PROFILES[0]
    worker.resolution = opentranscode_module.RESOLUTION_PRESETS[0]

    output_f = tmp_path / "output_archived.mkv"
    output_f.write_bytes(b"corrupt content")

    # ffprobe returns non-zero (corrupt file).
    monkeypatch.setattr(
        "subprocess.run",
        MagicMock(return_value=subprocess.CompletedProcess(
            args=["ffprobe"], returncode=1, stdout="", stderr="error",
        )),
    )

    assert worker._output_already_encoded(tmp_path / "source.mkv", output_f) is False


def test_skip_existing_returns_false_when_no_ffprobe(opentranscode_module, mock_env, tmp_path):
    """No ffprobe available → can't verify codec → don't skip (safe default)."""
    worker = make_minimal_worker(opentranscode_module, env=mock_env)
    worker.skip_existing = True
    worker.video_codec = opentranscode_module.VIDEO_CODECS[0]
    worker.audio_profile = opentranscode_module.AUDIO_PROFILES[0]
    worker.resolution = opentranscode_module.RESOLUTION_PRESETS[0]

    output_f = tmp_path / "output_archived.mkv"
    output_f.write_bytes(b"content")

    # Simulate no ffprobe.
    worker.env.ffprobe_path = None

    assert worker._output_already_encoded(tmp_path / "source.mkv", output_f) is False


def test_skip_existing_checks_resolution_when_scaling_requested(opentranscode_module, mock_env, tmp_path, monkeypatch):
    """When scaling is requested, output resolution must match the target."""
    worker = make_minimal_worker(opentranscode_module, env=mock_env)
    worker.skip_existing = True
    worker.video_codec = opentranscode_module.VIDEO_CODECS[0]  # AV1
    worker.audio_profile = opentranscode_module.AUDIO_PROFILES[0]  # Opus
    # Select a target resolution (720p = 1280x720, index 2).
    # The mock ffprobe returns 1920x1080, so they WON'T match → don't skip.
    worker.resolution = opentranscode_module.RESOLUTION_PRESETS[2]  # 720p

    output_f = tmp_path / "output_archived.mkv"
    output_f.write_bytes(b"content")

    # ffprobe says 1920x1080, but we want 1280x720 → mismatch → don't skip.
    monkeypatch.setattr(
        "subprocess.run",
        MagicMock(return_value=_ffprobe_result(_av1_opus_output())),
    )

    assert worker._output_already_encoded(tmp_path / "source.mkv", output_f) is False


# ── CLI flag tests ─────────────────────────────────────────────────────────

def test_cli_skip_existing_default_true():
    """Without --force-reencode, skip_existing defaults to True."""
    from opentranscode.cli import build_parser
    args = build_parser().parse_args([])
    assert args.skip_existing is True


def test_cli_force_reencode_sets_false():
    """--force-reencode sets skip_existing to False."""
    from opentranscode.cli import build_parser
    args = build_parser().parse_args(["--force-reencode"])
    assert args.skip_existing is False


def test_cli_skip_existing_explicit():
    """--skip-existing explicitly sets skip_existing to True."""
    from opentranscode.cli import build_parser
    args = build_parser().parse_args(["--skip-existing"])
    assert args.skip_existing is True


def test_launch_gui_signature_accepts_skip_existing():
    """launch_gui() accepts the skip_existing kwarg (v4.3.0)."""
    import inspect
    from opentranscode import launch_gui
    sig = inspect.signature(launch_gui)
    assert "skip_existing" in sig.parameters
    # Default must be True (skip by default).
    assert sig.parameters["skip_existing"].default is True
