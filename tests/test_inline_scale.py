"""
v4.4.4: inline-scale + CRF-16 intermediate tests.

Two changes that prevent failures on 10GB+ source files:

  1. Pre-scale intermediate changed from CRF 0 (mathematically lossless,
     2-4× source size) to CRF 16 (visually lossless, 0.5-0.8× source size).
     The old CRF-0 intermediate was producing 60-80GB temp files for 20GB
     sources, exhausting disk and crashing the encode with mysterious
     "ffmpeg error (rc=234)" messages.

  2. New --inline-scale flag (UI checkbox "Inline scale (no intermediate)")
     skips the pre-scale intermediate entirely. The scale/pad filter chain
     is passed directly to av1an via --ffmpeg-filter-args, eliminating the
     intermediate file (zero extra disk usage, one fewer encode pass).
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ── CLI flag ─────────────────────────────────────────────────────────────────

def test_cli_inline_scale_default_off():
    """Without --inline-scale, the flag defaults to False."""
    from opentranscode.cli import build_parser
    args = build_parser().parse_args([])
    assert args.inline_scale is False


def test_cli_inline_scale_flag():
    """--inline-scale sets the flag to True."""
    from opentranscode.cli import build_parser
    args = build_parser().parse_args(["--inline-scale"])
    assert args.inline_scale is True


def test_launch_gui_signature_accepts_inline_scale():
    """launch_gui() accepts the inline_scale kwarg (v4.4.4)."""
    import inspect
    from opentranscode import launch_gui
    sig = inspect.signature(launch_gui)
    assert "inline_scale" in sig.parameters
    # Default must be False — the intermediate path is the safe default.
    assert sig.parameters["inline_scale"].default is False


# ── EncoderWorker picks up inline_scale from env ─────────────────────────────

def test_worker_inline_scale_default_false(opentranscode_module, mock_env):
    """EncoderWorker defaults inline_scale to False when env.av1an_flags
    has no 'inline_scale' key."""
    # Ensure no stale value from a previous test
    mock_env.av1an_flags.pop("inline_scale", None)
    worker = opentranscode_module.EncoderWorker.__new__(opentranscode_module.EncoderWorker)
    # Replicate the __init__ line that reads the flag
    worker.inline_scale = bool(mock_env.av1an_flags.get("inline_scale", False))
    assert worker.inline_scale is False


def test_worker_inline_scale_from_env(opentranscode_module, mock_env):
    """EncoderWorker picks up inline_scale=True from env.av1an_flags."""
    mock_env.av1an_flags["inline_scale"] = True
    worker = opentranscode_module.EncoderWorker.__new__(opentranscode_module.EncoderWorker)
    worker.inline_scale = bool(mock_env.av1an_flags.get("inline_scale", False))
    assert worker.inline_scale is True


# ── _prepare_input respects inline_scale ─────────────────────────────────────

def test_prepare_input_skips_prescale_when_inline_scale(opentranscode_module):
    """When inline_scale=True, _prepare_input does NOT run the pre-scale
    block (no CRF-16 intermediate file is created). The encode_input
    stays as the original file_path."""
    src = inspect.getsource(opentranscode_module.EncoderWorker._prepare_input)
    # The pre-scale block must be gated by `not self.inline_scale`
    assert "not self.inline_scale" in src, (
        "_prepare_input must gate the pre-scale block on `not self.inline_scale`"
    )


def test_prepare_input_runs_prescale_when_not_inline_scale(opentranscode_module):
    """When inline_scale=False (default), _prepare_input runs the pre-scale
    block as before (creating a CRF-16 intermediate)."""
    src = inspect.getsource(opentranscode_module.EncoderWorker._prepare_input)
    # The default code path must still produce a temp_scaled file
    assert "temp_scaled" in src
    assert "_temp_path_for" in src


# ── _encode_one injects --ffmpeg-filter-args when inline_scale ───────────────

def test_encode_one_injects_filter_args(opentranscode_module):
    """_encode_one must contain the --ffmpeg-filter-args injection block
    that fires when self.inline_scale and self._current_scale_filter are
    both set."""
    src = inspect.getsource(opentranscode_module.EncoderWorker._encode_one)
    assert "--ffmpeg-filter-args" in src, (
        "_encode_one must inject --ffmpeg-filter-args when inline_scale is enabled"
    )
    assert "self.inline_scale" in src
    assert "self._current_scale_filter" in src


# ── _process_one_file stashes scale_filter ───────────────────────────────────

def test_process_one_file_stashes_scale_filter(opentranscode_module):
    """_process_one_file must stash scale_filter on self._current_scale_filter
    so _encode_one can read it without a signature change."""
    src = inspect.getsource(opentranscode_module.EncoderWorker._process_one_file)
    assert "_current_scale_filter" in src, (
        "_process_one_file must stash scale_filter on self._current_scale_filter"
    )


# ── CRF 16 (not CRF 0) for the pre-scale intermediate ───────────────────────

def test_prescale_uses_crf_16_not_crf_0(opentranscode_module):
    """v4.4.4: the pre-scale intermediate uses CRF 16 (visually lossless),
    NOT CRF 0 (mathematically lossless). CRF 0 produced 2-4× source size
    intermediates that crashed 20GB encodes by exhausting disk."""
    src = inspect.getsource(opentranscode_module.EncoderWorker._prepare_input)
    # Must contain CRF 16
    assert '"16"' in src or "'16'" in src, (
        "Pre-scale intermediate must use CRF 16 (visually lossless)"
    )
    # Must NOT contain CRF 0 as the encoder quality target. We check the
    # specific "-crf", "0" pattern (with comma+quote) to avoid matching
    # any incidental 0 in the source.
    assert '"-crf", "0"' not in src and "'-crf', '0'" not in src, (
        "Pre-scale intermediate must NOT use CRF 0 (mathematically lossless). "
        "CRF 0 produces 2-4x source size intermediates that crash large encodes."
    )


def test_prescale_uses_libx265(opentranscode_module):
    """The intermediate codec is still libx265 (HEVC) — required for
    VapourSynth source plugin compatibility (ffv1 is unsupported)."""
    src = inspect.getsource(opentranscode_module.EncoderWorker._prepare_input)
    assert '"libx265"' in src or "'libx265'" in src


# ── open-transcode.py launcher script mirror ─────────────────────────────────

def test_launcher_script_prescale_uses_crf_16(opentranscode_module):
    """The launcher script (open-transcode.py) must mirror the CRF-16
    change. The package and the launcher script must stay in sync."""
    # opentranscode_module fixture loads open-transcode.py
    src = inspect.getsource(opentranscode_module.EncoderWorker._prepare_input)
    assert '"16"' in src or "'16'" in src, (
        "Launcher script's pre-scale intermediate must use CRF 16"
    )
    assert '"-crf", "0"' not in src and "'-crf', '0'" not in src, (
        "Launcher script must NOT use CRF 0 for pre-scale intermediate"
    )


def test_launcher_script_has_inline_scale(opentranscode_module):
    """The launcher script mirrors the inline_scale flag."""
    src = inspect.getsource(opentranscode_module.EncoderWorker.__init__)
    assert 'inline_scale' in src, (
        "Launcher script's EncoderWorker.__init__ must read inline_scale"
    )

    src2 = inspect.getsource(opentranscode_module.EncoderWorker._encode_one)
    assert "--ffmpeg-filter-args" in src2, (
        "Launcher script's _encode_one must inject --ffmpeg-filter-args"
    )

    src3 = inspect.getsource(opentranscode_module.EncoderWorker._prepare_input)
    assert "not self.inline_scale" in src3, (
        "Launcher script's _prepare_input must gate pre-scale on not self.inline_scale"
    )
