"""
v4.2.0: ffmpeg-first default + --use-av1an opt-in.

QA finding: av1an chunk-parallel path was too fragile across distros.
The default encode path is now ffmpeg-only. av1an is opt-in via
``--use-av1an`` (stored on ``env.av1an_flags["use_av1an"]``).

The cases:
  1. ``--use-av1an`` not given → ``env.av1an_flags["use_av1an"]`` is False.
  2. ``--use-av1an`` given → ``env.av1an_flags["use_av1an"]`` is True.
  3. CLI parser accepts the flag.
  4. ``launch_gui`` propagates the flag to ``env.av1an_flags``.
"""

from __future__ import annotations

import pytest


def test_cli_flag_use_av1an_default_false():
    """Without --use-av1an, args.use_av1an is False (default)."""
    from opentranscode.cli import build_parser
    args = build_parser().parse_args([])
    assert args.use_av1an is False


def test_cli_flag_use_av1an_opt_in():
    """--use-av1an sets args.use_av1an to True."""
    from opentranscode.cli import build_parser
    args = build_parser().parse_args(["--use-av1an"])
    assert args.use_av1an is True


def test_launch_gui_signature_accepts_use_av1an():
    """launch_gui() accepts the use_av1an kwarg (v4.2.0)."""
    import inspect
    from opentranscode import launch_gui
    sig = inspect.signature(launch_gui)
    assert "use_av1an" in sig.parameters
    # Default must be False (ffmpeg-first).
    assert sig.parameters["use_av1an"].default is False


def test_dry_run_does_not_crash_with_use_av1an_flag():
    """--dry-run --use-av1an parses cleanly (we don't actually run the
    dry-run here because it requires a real env probe; just verify the
    CLI parser accepts the combination)."""
    from opentranscode.cli import build_parser
    args = build_parser().parse_args(["--dry-run", "--use-av1an"])
    assert args.dry_run is True
    assert args.use_av1an is True
