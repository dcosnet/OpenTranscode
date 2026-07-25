"""
Subtitle stream-selection tests for ``EncoderWorker._find_subtitle_stream``.

QA finding: OTC-006 (subtitle muxing logic).

``_find_subtitle_stream`` runs ffprobe on the source file and scans the
subtitle streams for one matching the requested language code. It prefers
"forced" disposition tracks (e.g. forced narrative subtitles for foreign
dialog) over plain tracks of the same language. Returns ``(stream_index,
codec_name)`` or ``(None, "")`` if no match.

The 3 cases:
  - 2 eng subtitle streams, one forced -> returns the forced one.
  - 1 non-forced eng subtitle -> falls back to that match.
  - Only fra subtitles (no eng) -> returns (None, "").

All cases mock ``subprocess.run`` so no real ffprobe is required.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from conftest import make_minimal_worker


def _ffprobe_completed_process(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["ffprobe"], returncode=0,
        stdout=json.dumps(payload), stderr="",
    )


def test_finds_forced_subtitle(opentranscode_module, mock_env, monkeypatch):
    """2 eng subtitle streams; the one with disposition.forced=1 wins."""
    ffprobe_json = {
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264"},
            {"index": 1, "codec_type": "audio", "codec_name": "aac"},
            # First eng subtitle: non-forced.
            {"index": 2, "codec_type": "subtitle", "codec_name": "subrip",
             "tags": {"language": "eng"},
             "disposition": {"forced": 0, "default": 1}},
            # Second eng subtitle: forced (e.g. forced narrative).
            {"index": 3, "codec_type": "subtitle", "codec_name": "subrip",
             "tags": {"language": "eng"},
             "disposition": {"forced": 1, "default": 0}},
        ],
        "format": {"duration": "120.0"},
    }
    monkeypatch.setattr(
        "subprocess.run",
        MagicMock(return_value=_ffprobe_completed_process(ffprobe_json)),
    )

    worker = make_minimal_worker(opentranscode_module, env=mock_env)
    idx, codec = worker._find_subtitle_stream(
        Path("/fake/movie.mkv"), lang="eng",
    )

    assert idx == 3  # forced track wins
    assert codec == "subrip"


def test_falls_back_to_any_match(opentranscode_module, mock_env, monkeypatch):
    """1 non-forced eng subtitle -> returns it (no forced track to prefer)."""
    ffprobe_json = {
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264"},
            {"index": 1, "codec_type": "audio", "codec_name": "aac"},
            {"index": 2, "codec_type": "subtitle", "codec_name": "ass",
             "tags": {"language": "eng"},
             "disposition": {"forced": 0, "default": 1}},
        ],
        "format": {"duration": "120.0"},
    }
    monkeypatch.setattr(
        "subprocess.run",
        MagicMock(return_value=_ffprobe_completed_process(ffprobe_json)),
    )

    worker = make_minimal_worker(opentranscode_module, env=mock_env)
    idx, codec = worker._find_subtitle_stream(
        Path("/fake/movie.mkv"), lang="eng",
    )

    assert idx == 2
    assert codec == "ass"


def test_returns_none_when_no_match(opentranscode_module, mock_env, monkeypatch):
    """Only fra subtitles, lang=eng requested -> returns (None, "")."""
    ffprobe_json = {
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264"},
            {"index": 1, "codec_type": "audio", "codec_name": "aac"},
            {"index": 2, "codec_type": "subtitle", "codec_name": "subrip",
             "tags": {"language": "fra"},
             "disposition": {"forced": 0, "default": 1}},
        ],
        "format": {"duration": "120.0"},
    }
    monkeypatch.setattr(
        "subprocess.run",
        MagicMock(return_value=_ffprobe_completed_process(ffprobe_json)),
    )

    worker = make_minimal_worker(opentranscode_module, env=mock_env)
    idx, codec = worker._find_subtitle_stream(
        Path("/fake/movie.mkv"), lang="eng",
    )

    assert idx is None
    assert codec == ""
