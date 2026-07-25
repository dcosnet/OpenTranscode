"""
Package-structure tests — verify the opentranscode/ package imports cleanly
and exposes the expected public API.

These tests run WITHOUT PySide6 installed (the conftest.py installs stubs).
They verify that the v4 package split (QA item v4-03) preserved all the
public symbols that were in the v3 launcher script.
"""
import importlib
import sys
from pathlib import Path

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Public API surface — every symbol here MUST be importable from the package
# ─────────────────────────────────────────────────────────────────────────────

EXPECTED_TOP_LEVEL_EXPORTS = {
    "__version__",
    "__author__",
    "__license__",
    "build_parser",
    "main",
    "launch_gui",
}

EXPECTED_SUBMODULES = {
    "opentranscode.cli",
    "opentranscode.codec_profiles",
    "opentranscode.license_registry",
    "opentranscode.cpu_topology",
    "opentranscode.distro_probe",
    "opentranscode.env_probe",
    "opentranscode.ffprobe_utils",
    "opentranscode.temp_manager",
    "opentranscode.encoder_worker",
    "opentranscode.source_builder",
    "opentranscode.ui_theme",
    "opentranscode.ui_window",
    "opentranscode.widgets",
    "opentranscode.widgets.radio_knob",
}

EXPECTED_CODEC_PROFILES_EXPORTS = {
    "VideoCodecProfile",
    "AudioProfile",
    "ContainerProfile",
    "ResolutionProfile",
    "VIDEO_CODECS",
    "AUDIO_PROFILES",
    "CONTAINER_PROFILES",
    "RESOLUTION_PRESETS",
    "SUBTITLE_OPTIONS",
    "DEFAULT_INPUT_EXTENSIONS",
    "FFMPEG_LIB_KEY_MAP",
    "ffmpeg_lib_key_for",
}

EXPECTED_ENV_PROBE_EXPORTS = {
    "EnvProbe",
    "probe_environment",
    "_av1an_vsscript_smoke_test",
    "_detect_av1an_svt_encoder",
}

EXPECTED_ENCODER_WORKER_EXPORTS = {
    "EncoderWorker",
}

EXPECTED_DISTRO_PROBE_EXPORTS = {
    "DistroProfile",
    "DISTRO_REGISTRY",
    "detect_distro",
}


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestPackageMetadata:
    def test_version_is_pep440_compliant(self):
        import opentranscode
        v = opentranscode.__version__
        # PEP 440: X.Y.Z or X.Y.Z.devN or X.Y.ZrcN etc.
        assert isinstance(v, str)
        parts = v.split(".")
        assert len(parts) >= 3, f"Version '{v}' should have at least major.minor.patch"
        assert all(parts[0].isdigit() and parts[1].isdigit() and parts[2].split("rc")[0].split("dev")[0].isdigit() or
                   parts[2] == "0" for part in parts[:3]), \
            f"Version '{v}' should be PEP 440 numeric"

    def test_author_is_set(self):
        import opentranscode
        assert opentranscode.__author__
        assert isinstance(opentranscode.__author__, str)

    def test_license_is_agpl(self):
        import opentranscode
        assert "AGPL" in opentranscode.__license__

    def test_all_is_defined(self):
        import opentranscode
        assert hasattr(opentranscode, "__all__")
        assert isinstance(opentranscode.__all__, list)


class TestSubmodulesImportable:
    """Every submodule in the package must import cleanly."""

    @pytest.mark.parametrize("modname", sorted(EXPECTED_SUBMODULES))
    def test_submodule_imports(self, modname):
        mod = importlib.import_module(modname)
        assert mod is not None
        # The module's __name__ should match what we asked for
        assert mod.__name__ == modname


class TestPublicAPI:
    """Verify the expected public symbols are present in each module."""

    def test_codec_profiles_exports(self):
        from opentranscode import codec_profiles
        for name in EXPECTED_CODEC_PROFILES_EXPORTS:
            assert hasattr(codec_profiles, name), \
                f"codec_profiles.{name} missing"

    def test_env_probe_exports(self):
        from opentranscode import env_probe
        for name in EXPECTED_ENV_PROBE_EXPORTS:
            assert hasattr(env_probe, name), \
                f"env_probe.{name} missing"

    def test_encoder_worker_exports(self):
        from opentranscode import encoder_worker
        for name in EXPECTED_ENCODER_WORKER_EXPORTS:
            assert hasattr(encoder_worker, name)

    def test_distro_probe_exports(self):
        from opentranscode import distro_probe
        for name in EXPECTED_DISTRO_PROBE_EXPORTS:
            assert hasattr(distro_probe, name)

    def test_video_codecs_table_populated(self):
        from opentranscode.codec_profiles import VIDEO_CODECS
        assert len(VIDEO_CODECS) >= 3, "Should have at least 3 video codecs (AV1, VP9, x265)"
        labels = [c.label for c in VIDEO_CODECS]
        assert any("AV1" in l for l in labels)
        assert any("VP9" in l for l in labels)
        assert any("x265" in l or "HEVC" in l for l in labels)

    def test_audio_profiles_have_ffmpeg_encoder_name(self):
        """v3-02 (OTC-012): every AudioProfile must have ffmpeg_encoder_name set."""
        from opentranscode.codec_profiles import AUDIO_PROFILES
        for ap in AUDIO_PROFILES:
            assert ap.ffmpeg_encoder_name, \
                f"AudioProfile '{ap.label}' has empty ffmpeg_encoder_name (OTC-012 violation)"

    def test_distro_registry_has_six_entries(self):
        """v3-04: DISTRO_REGISTRY should have 6 entries (arch, fedora, rhel, suse, nixos, debian)."""
        from opentranscode.distro_probe import DISTRO_REGISTRY
        families = {e.family for e in DISTRO_REGISTRY}
        assert "arch" in families
        assert "debian" in families
        assert "redhat" in families
        assert "suse" in families
        assert "nixos" in families
        assert len(DISTRO_REGISTRY) >= 6


class TestCLIParser:
    """Verify the CLI argument parser works."""

    def test_build_parser_returns_argparse(self):
        import argparse
        from opentranscode.cli import build_parser
        p = build_parser()
        assert isinstance(p, argparse.ArgumentParser)

    def test_version_flag(self):
        from opentranscode.cli import build_parser
        p = build_parser()
        args = p.parse_args(["--version"])
        assert args.version is True

    def test_dry_run_flag(self):
        from opentranscode.cli import build_parser
        p = build_parser()
        args = p.parse_args(["--dry-run"])
        assert args.dry_run is True

    def test_verify_only_flag(self):
        from opentranscode.cli import build_parser
        p = build_parser()
        args = p.parse_args(["--verify-only", "/tmp/test.mp4"])
        assert args.verify_only == "/tmp/test.mp4"

    def test_no_flags_returns_none(self):
        from opentranscode.cli import build_parser
        p = build_parser()
        args = p.parse_args([])
        assert args.version is False
        assert args.dry_run is False
        assert args.verify_only is None


class TestFFmpegLibKeyMap:
    """v3-01 (OTC-007): verify the single source of truth for ffmpeg lib key mapping."""

    def test_map_has_all_codecs(self):
        from opentranscode.codec_profiles import FFMPEG_LIB_KEY_MAP
        assert "libsvtav1" in FFMPEG_LIB_KEY_MAP
        assert "libaom-av1" in FFMPEG_LIB_KEY_MAP
        assert "libvpx-vp9" in FFMPEG_LIB_KEY_MAP
        assert "libx265" in FFMPEG_LIB_KEY_MAP

    def test_helper_returns_correct_keys(self):
        from opentranscode.codec_profiles import ffmpeg_lib_key_for
        assert ffmpeg_lib_key_for("libsvtav1") == "libsvtav1"
        assert ffmpeg_lib_key_for("libaom-av1") == "libaom"
        assert ffmpeg_lib_key_for("libvpx-vp9") == "libvpx"
        assert ffmpeg_lib_key_for("libx265") == "libx265"

    def test_helper_returns_input_for_unknown(self):
        """Forward-compat: unknown encoders fall back to themselves."""
        from opentranscode.codec_profiles import ffmpeg_lib_key_for
        assert ffmpeg_lib_key_for("libfuturecodec") == "libfuturecodec"


class TestEntryPoints:
    """Verify the entry points declared in pyproject.toml are reachable."""

    def test_main_callable_from_package(self):
        from opentranscode import main
        assert callable(main)

    def test_launch_gui_callable(self):
        from opentranscode import launch_gui
        assert callable(launch_gui)

    def test_main_module_runs(self, capsys):
        """`python -m opentranscode --version` should print version and exit 0."""
        import subprocess
        import sys
        result = subprocess.run(
            [sys.executable, "-m", "opentranscode", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert "4.5.0" in result.stdout
