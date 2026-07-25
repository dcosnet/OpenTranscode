"""
Encoder pipeline tests for ``EncoderWorker._validate_file``.

QA finding: OTC-002 (pre-encode validation coverage).

``_validate_file`` runs ffprobe on a candidate input and returns a 4-tuple
``(skip, info, src_w, src_h)``. It must SKIP files that have no video stream
or are too short (<0.5s) — otherwise the av1an/ffmpeg encode would crash
mid-pipeline or hang on a degenerate input.

The 3 cases here cover:
  - No video stream (audio-only file mistakenly placed in input dir).
  - Sub-0.5s duration (truncated / corrupted capture).
  - Valid 1080p video — should pass through with src_w/src_h extracted.

All 3 mock ``subprocess.run`` so the tests run without a real ffprobe
binary. The worker is instantiated via ``__new__`` (no QThread.start).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from conftest import make_minimal_worker


def _ffprobe_completed_process(payload: dict) -> subprocess.CompletedProcess:
    """Wrap a dict as a ffprobe-style CompletedProcess (stdout=JSON)."""
    return subprocess.CompletedProcess(
        args=["ffprobe"], returncode=0,
        stdout=json.dumps(payload), stderr="",
    )


def test_validate_file_skips_no_video_stream(opentranscode_module, mock_env, monkeypatch):
    """ffprobe returns JSON with no video stream -> skip=True."""
    ffprobe_json = {
        "streams": [
            {"index": 0, "codec_type": "audio", "codec_name": "aac"},
        ],
        "format": {"duration": "10.0", "name": "mov,mp4,m4a,3gp,3g2,mj2"},
    }
    monkeypatch.setattr(
        "subprocess.run",
        MagicMock(return_value=_ffprobe_completed_process(ffprobe_json)),
    )

    worker = make_minimal_worker(opentranscode_module, env=mock_env)
    skip, info, src_w, src_h = worker._validate_file(Path("/fake/audio-only.mkv"))

    assert skip is True
    assert info is None
    assert src_w is None
    assert src_h is None
    # The skip path increments fail_count so the queue summary is accurate.
    assert worker.fail_count == 1


def test_validate_file_skips_short_duration(opentranscode_module, mock_env, monkeypatch):
    """ffprobe returns duration=0.3 (<0.5s threshold) -> skip=True."""
    ffprobe_json = {
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264",
             "width": 1920, "height": 1080},
        ],
        "format": {"duration": "0.3"},
    }
    monkeypatch.setattr(
        "subprocess.run",
        MagicMock(return_value=_ffprobe_completed_process(ffprobe_json)),
    )

    worker = make_minimal_worker(opentranscode_module, env=mock_env)
    skip, info, src_w, src_h = worker._validate_file(Path("/fake/short.mkv"))

    assert skip is True
    assert info is None
    assert src_w is None
    assert src_h is None
    assert worker.fail_count == 1


def test_validate_file_accepts_valid_video(opentranscode_module, mock_env, monkeypatch):
    """ffprobe returns a valid video stream + duration=10.0
    -> skip=False, src_w=1920, src_h=1080.
    """
    ffprobe_json = {
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264",
             "width": 1920, "height": 1080},
            {"index": 1, "codec_type": "audio", "codec_name": "aac"},
        ],
        "format": {"duration": "10.0"},
    }
    monkeypatch.setattr(
        "subprocess.run",
        MagicMock(return_value=_ffprobe_completed_process(ffprobe_json)),
    )

    worker = make_minimal_worker(opentranscode_module, env=mock_env)
    skip, info, src_w, src_h = worker._validate_file(Path("/fake/valid.mkv"))

    assert skip is False
    assert info is not None
    assert src_w == 1920
    assert src_h == 1080
    assert worker.fail_count == 0
