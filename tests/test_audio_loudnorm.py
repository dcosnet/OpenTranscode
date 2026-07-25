"""
Audio loudness analysis tests for ``EncoderWorker._analyze_audio_loudness``.

QA finding: OTC-005 (dual-pass loudnorm gain computation).

``_analyze_audio_loudness`` runs ffmpeg's ``loudnorm`` filter in
analysis-only mode, parses the JSON stats block from stderr, and computes
the dB gain needed to bring the file's integrated loudness up to (or down
to) the user's target LUFS (knob value). It also clamps the gain so the
projected true peak stays below a 15%-headroom ceiling under ``target_tp``
(default -1.5 dBTP).

The 4 cases:
  - Valid JSON, low input loudness -> +9.0 dB gain (no clamp).
  - Valid JSON, hot true peak -> gain clamped to keep peak under ceiling.
  - Silent input (input_i <= -70) -> returns None (no normalization needed).
  - No JSON in stderr (loudnorm parse failure) -> returns None.

All cases mock ``subprocess.run`` so no real ffmpeg is required.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from conftest import make_minimal_worker


def _loudnorm_stderr(input_i: str, input_tp: str,
                     target_tp: str = "-1.5") -> str:
    """Build a realistic ffmpeg loudnorm stderr block containing a JSON stats.

    ffmpeg prints a bunch of progress lines, then a JSON block at the end.
    The open-transcode.py parser uses ``re.search(r'\\{[^{}]*"input_i"[^{}]*\\}', stderr,
    re.DOTALL)`` to find the JSON — flat (no nested braces) is required.
    """
    return (
        f"[Parsed_loudnorm_0 @ 0x7f] Changing target table from "
        f"I=-70 to I={input_i}\n"
        f"...\n"
        f"{{\n"
        f"\t\"input_i\" : \"{input_i}\",\n"
        f"\t\"input_tp\" : \"{input_tp}\",\n"
        f"\t\"input_lra\" : \"7.0\",\n"
        f"\t\"input_thresh\" : \"-33.0\",\n"
        f"\t\"output_i\" : \"-14.0\",\n"
        f"\t\"output_tp\" : \"{target_tp}\",\n"
        f"\t\"output_lra\" : \"7.0\",\n"
        f"\t\"output_thresh\" : \"-24.0\",\n"
        f"\t\"normalization_type\" : \"linear\",\n"
        f"\t\"target_offset\" : \"-0.0\"\n"
        f"}}\n"
    )


def test_loudnorm_parses_json_stats(opentranscode_module, mock_env, monkeypatch):
    """input_i=-23.0, target_lufs=-14 -> gain_db = +9.0 (no clamp).

    The file's true peak (-15.0 dBTP) is low enough that even with +9.0 dB
    of gain the projected peak (-6.0 dBTP) stays well under the
    15%-headroom ceiling (-1.275 dBTP), so no clamping happens.
    """
    stderr = _loudnorm_stderr(input_i="-23.0", input_tp="-15.0")
    monkeypatch.setattr(
        "subprocess.run",
        MagicMock(return_value=subprocess.CompletedProcess(
            args=["ffmpeg"], returncode=0, stdout="", stderr=stderr,
        )),
    )

    # audio_level_db IS the target LUFS (see _analyze_audio_loudness).
    worker = make_minimal_worker(opentranscode_module, env=mock_env, audio_level_db=-14.0)
    gain = worker._analyze_audio_loudness(Path("/fake/audio.mkv"))

    assert gain is not None
    # -14.0 - (-23.0) = +9.0
    assert gain == pytest.approx(9.0, abs=0.01)


def test_loudnorm_clamps_peak(opentranscode_module, mock_env, monkeypatch):
    """input_i=-14, input_tp=-0.5, target_lufs=-14 -> gain clamped.

    The file's integrated loudness already equals the target (-14 LUFS), so
    the raw gain would be 0 dB. But the file's true peak (-0.5 dBTP) is
    already above the 15%-headroom ceiling of -1.275 dBTP, so the gain is
    clamped DOWN to bring the peak under the ceiling.

    ceiling = -1.5 + (|-1.5| * 0.15) = -1.5 + 0.225 = -1.275
    clamped_gain = -1.275 - (-0.5) = -0.775 dB
    """
    stderr = _loudnorm_stderr(input_i="-14.0", input_tp="-0.5")
    monkeypatch.setattr(
        "subprocess.run",
        MagicMock(return_value=subprocess.CompletedProcess(
            args=["ffmpeg"], returncode=0, stdout="", stderr=stderr,
        )),
    )

    worker = make_minimal_worker(opentranscode_module, env=mock_env, audio_level_db=-14.0)
    gain = worker._analyze_audio_loudness(Path("/fake/hot_peak.mkv"))

    assert gain is not None
    # The clamped gain must be NEGATIVE (attenuation) and bring the peak
    # under the -1.275 ceiling.
    assert gain < 0, (
        f"Expected clamped (negative) gain for input_tp=-0.5, got {gain}"
    )
    assert gain == pytest.approx(-0.775, abs=0.01)
    # Verify the projected peak is at or under the ceiling.
    projected_peak = -0.5 + gain
    assert projected_peak <= -1.275 + 1e-6


def test_loudnorm_returns_none_for_silent_input(opentranscode_module, mock_env, monkeypatch):
    """input_i=-80 (silent) -> returns None (no normalization needed).

    A silent file has no audible loudness to normalize; applying gain would
    just amplify noise. The open-transcode.py code special-cases ``input_i <= -70``.
    """
    stderr = _loudnorm_stderr(input_i="-80.0", input_tp="-99.0")
    monkeypatch.setattr(
        "subprocess.run",
        MagicMock(return_value=subprocess.CompletedProcess(
            args=["ffmpeg"], returncode=0, stdout="", stderr=stderr,
        )),
    )

    worker = make_minimal_worker(opentranscode_module, env=mock_env, audio_level_db=-14.0)
    gain = worker._analyze_audio_loudness(Path("/fake/silent.mkv"))

    assert gain is None


def test_loudnorm_returns_none_on_parse_failure(opentranscode_module, mock_env, monkeypatch):
    """No JSON block in stderr -> returns None (falls back to knob value).

    If ffmpeg was killed, hit a parse error, or printed an unexpected
    format, the regex ``\\{[^{}]*"input_i"[^{}]*\\}`` will not match.
    The caller falls back to the knob's static dB value (see
    ``_encode_one``).
    """
    stderr = (
        "[Parsed_loudnorm_0 @ 0x7f] Estimating noise...\n"
        "ffmpeg exited with code 1 — no JSON stats printed.\n"
    )
    monkeypatch.setattr(
        "subprocess.run",
        MagicMock(return_value=subprocess.CompletedProcess(
            args=["ffmpeg"], returncode=0, stdout="", stderr=stderr,
        )),
    )

    worker = make_minimal_worker(opentranscode_module, env=mock_env, audio_level_db=-14.0)
    gain = worker._analyze_audio_loudness(Path("/fake/broken.mkv"))

    assert gain is None
