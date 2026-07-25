"""
v4.2.1: --verbose flag (default False).

QA finding: v4.2.0's log output was too noisy. Default is now quiet
(per-file success/fail + final summary). --verbose re-enables the
tech-detail log output.
"""

from __future__ import annotations


def test_cli_flag_verbose_default_false():
    """Without --verbose, args.verbose is False (default = quiet)."""
    from opentranscode.cli import build_parser
    args = build_parser().parse_args([])
    assert args.verbose is False


def test_cli_flag_verbose_opt_in():
    """--verbose sets args.verbose to True."""
    from opentranscode.cli import build_parser
    args = build_parser().parse_args(["--verbose"])
    assert args.verbose is True


def test_launch_gui_signature_accepts_verbose():
    """launch_gui() accepts the verbose kwarg (v4.2.1)."""
    import inspect
    from opentranscode import launch_gui
    sig = inspect.signature(launch_gui)
    assert "verbose" in sig.parameters
    # Default must be False (quiet by default).
    assert sig.parameters["verbose"].default is False
