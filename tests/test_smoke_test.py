"""
Smoke-test coverage for ``_av1an_vsscript_smoke_test``.

QA finding: OTC-001 (critical).

The v1 implementation of the av1an VSScript smoke test returned ``True`` for
any non-VSScript failure, masking real bugs (missing encoder binary, concat-
method mismatch, av1an panic, etc.). The pre-flight check therefore reported
"OK" and the per-file loop then failed for every file — the
"chunks but never saves a file" symptom.

The open-transcode.py implementation (this is what we're testing) classifies failure modes
and returns ``False`` for unknown failures. The critical regression test is
``test_smoke_returns_false_on_unknown_failure``: it feeds the function a
generic rc=1 + non-VSScript stderr and verifies the function now returns
``False`` (the v1 bug was returning ``True`` here).

All 5 cases run without a real av1an / ffmpeg install: ``subprocess.run`` is
mocked via ``monkeypatch.setattr("subprocess.run", ...)`` and the smoke-test
function's ``test_in.exists()`` / ``test_out.exists()`` checks are satisfied
by the mock side-effect creating the expected files at the in/out paths that
the function passes on the command line.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_smoke_side_effect(scenario: str):
    """Build a side_effect callable for ``subprocess.run``.

    The smoke-test function calls ``subprocess.run`` exactly twice:
      1. ffmpeg gen-cmd — last argument is the output path (``test_in``).
      2. av1an cmd      — output path follows ``-o``.

    For "happy" the side_effect creates both files so the function's
    ``Path.exists()`` checks pass. For failure scenarios it creates only
    ``test_in`` (so the function proceeds past the gen step) and returns
    the appropriate ``CompletedProcess`` for the av1an call.
    """
    call_count = [0]

    def side_effect(cmd, *args, **kwargs):
        i = call_count[0]
        call_count[0] += 1

        if i == 0:
            # ffmpeg gen-cmd — always rc=0; create the test_in file so
            # `test_in.exists()` returns True inside the smoke-test.
            Path(cmd[-1]).write_bytes(b"\x00fake-video\x00")
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr="",
            )

        # i == 1 — av1an cmd
        if scenario == "happy":
            # Create test_out so `test_out.exists()` returns True.
            out_idx = cmd.index("-o") + 1
            Path(cmd[out_idx]).write_bytes(b"\x00fake-encode\x00")
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr="",
            )
        if scenario == "vsscript_incompat":
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="",
                stderr="Error: Failed to get VSScript API. ABI mismatch.",
            )
        if scenario == "invalid_encoder":
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="",
                stderr="error: invalid value 'foo' for '--encoder <ENCODER>'",
            )
        if scenario == "unknown_failure":
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="some av1an stdout",
                stderr="panic at src/encode.rs:42\nunknown failure mode",
            )
        if scenario == "timeout":
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)
        raise ValueError(f"unknown scenario: {scenario!r}")

    return side_effect


# ─────────────────────────────────────────────────────────────────────────────
#  Test cases
# ─────────────────────────────────────────────────────────────────────────────

AV1AN_FLAGS = {
    "worker": "--workers",
    "video_params": "--video-params",
    "audio_params": "--audio-params",
    "concat_method": "ffmpeg",
    "has_chunk_method": True,
}


def test_smoke_returns_true_on_success(opentranscode_module, monkeypatch):
    """Happy path: ffmpeg gen rc=0 + av1an rc=0 + output file exists
    -> returns ``(True, "av1an VSScript init OK")``.
    """
    monkeypatch.setattr(
        "subprocess.run",
        MagicMock(side_effect=_make_smoke_side_effect("happy")),
    )

    ok, detail = opentranscode_module._av1an_vsscript_smoke_test(
        av1an_bin="/fake/av1an",
        ffmpeg_bin="/fake/ffmpeg",
        av1an_flags=AV1AN_FLAGS,
        svt_name="svt_av1",
        timeout=5,
    )

    assert ok is True
    assert detail == "av1an VSScript init OK"


def test_smoke_returns_false_on_vsscript_incompat(opentranscode_module, monkeypatch):
    """stderr contains "Failed to get VSScript API"
    -> returns ``(False, "VSScript_API_INCOMPAT")``.
    """
    monkeypatch.setattr(
        "subprocess.run",
        MagicMock(side_effect=_make_smoke_side_effect("vsscript_incompat")),
    )

    ok, detail = opentranscode_module._av1an_vsscript_smoke_test(
        av1an_bin="/fake/av1an",
        ffmpeg_bin="/fake/ffmpeg",
        av1an_flags=AV1AN_FLAGS,
        svt_name="svt_av1",
        timeout=5,
    )

    assert ok is False
    assert detail == "VSScript_API_INCOMPAT"


def test_smoke_returns_false_on_invalid_encoder(opentranscode_module, monkeypatch):
    """stderr contains both "invalid value" and "--encoder"
    -> returns ``(False, "INVALID_ENCODER: ...")``.
    """
    monkeypatch.setattr(
        "subprocess.run",
        MagicMock(side_effect=_make_smoke_side_effect("invalid_encoder")),
    )

    ok, detail = opentranscode_module._av1an_vsscript_smoke_test(
        av1an_bin="/fake/av1an",
        ffmpeg_bin="/fake/ffmpeg",
        av1an_flags=AV1AN_FLAGS,
        svt_name="svt_av1",
        timeout=5,
    )

    assert ok is False
    assert detail.startswith("INVALID_ENCODER:")
    assert "invalid value" in detail
    assert "--encoder" in detail


def test_smoke_returns_false_on_unknown_failure(opentranscode_module, monkeypatch):
    """**CRITICAL OTC-001 REGRESSION TEST**.

    A generic rc=1 with non-VSScript stderr MUST return ``False``. The v1
    implementation returned ``True`` here, masking real bugs (missing encoder
    binary, concat-method mismatch, av1an panic, etc.) and causing the
    "chunks but never saves a file" symptom in production.

    open-transcode.py must classify this as ``SMOKE_FAIL`` so the caller can offer ffmpeg
    fallback or abort with an actionable message (SEI CERT ERR01-C: never
    mask a failure as success).
    """
    monkeypatch.setattr(
        "subprocess.run",
        MagicMock(side_effect=_make_smoke_side_effect("unknown_failure")),
    )

    ok, detail = opentranscode_module._av1an_vsscript_smoke_test(
        av1an_bin="/fake/av1an",
        ffmpeg_bin="/fake/ffmpeg",
        av1an_flags=AV1AN_FLAGS,
        svt_name="svt_av1",
        timeout=5,
    )

    # The whole point of OTC-001: this MUST be False, never True.
    assert ok is False, (
        "OTC-001 REGRESSION: smoke test returned True for an unknown "
        "failure. v1 had this bug and it caused 'chunks but never saves a "
        "file' in production. open-transcode.py must return False here."
    )
    assert detail.startswith("SMOKE_FAIL"), (
        f"Expected SMOKE_FAIL detail prefix, got: {detail!r}"
    )
    assert "rc=1" in detail
    # open-transcode.py includes the FULL stderr (not just the tail) so the user can see
    # the actual error and the diagnostic patterns can match on it.
    assert "unknown failure mode" in detail


def test_smoke_returns_false_on_timeout(opentranscode_module, monkeypatch):
    """``subprocess.TimeoutExpired`` raised
    -> returns ``(False, "SMOKE_TIMEOUT: ...")``.

    open-transcode.py does NOT mask a timeout as success — a hanging av1an is a real
    failure that the user must be told about (SEI CERT ERR01-C).
    """
    monkeypatch.setattr(
        "subprocess.run",
        MagicMock(side_effect=_make_smoke_side_effect("timeout")),
    )

    ok, detail = opentranscode_module._av1an_vsscript_smoke_test(
        av1an_bin="/fake/av1an",
        ffmpeg_bin="/fake/ffmpeg",
        av1an_flags=AV1AN_FLAGS,
        svt_name="svt_av1",
        timeout=5,
    )

    assert ok is False
    assert detail.startswith("SMOKE_TIMEOUT:")
    assert "5s" in detail or "5" in detail
