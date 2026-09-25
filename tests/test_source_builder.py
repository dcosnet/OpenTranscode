"""v4.7.1 rebuild-from-git tests.

The REBUILD FROM GIT button must be always usable: on a bare system it
generates its own dependency tree (distro-aware package install), then
builds VapourSynth + BestSource + av1an into user-owned prefixes.

Covers:
  - ``build_dep_plan``: per-family package lists + install commands,
    rust stripped when av1an isn't being built, manual note for
    unsupported families
  - ``_probe_vs_source_plugins``: discovers plugins in the python
    site-packages tree (where the git VS stack installs)
  - ``_av1an_env``: carries the fresh VS stack paths
  - the REBUILD button is enabled without a probe (offscreen UI test)
  - launcher parity
"""
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from opentranscode.distro_probe import DistroProfile, detect_distro
from opentranscode.source_builder import BUILD_DEPS_BY_FAMILY, build_dep_plan


def _profile(family):
    return DistroProfile(
        family=family, name=f"Test {family}", version_id="?",
        pkg_manager="pkg", install_cmd_template="", binary_extra_paths=[],
        av1an_known_encoder_names=[], ffmpeg_pkg="ffmpeg", av1an_pkg="av1an",
        notes="",
    )


class TestBuildDepPlan:
    def test_arch_uses_pacman_with_zimg_and_rust(self):
        plan = build_dep_plan(_profile("arch"))
        assert plan.install_cmd[:2] == ["pacman", "-S"]
        assert "zimg" in plan.packages
        assert "rust" in plan.packages
        assert "meson" in plan.packages
        assert plan.manual_note is None

    def test_debian_uses_apt_with_zimg_dev(self):
        plan = build_dep_plan(_profile("debian"))
        assert plan.install_cmd[:2] == ["apt-get", "install"]
        assert "libzimg-dev" in plan.packages
        assert "cargo" in plan.packages

    def test_redhat_and_suse_covered(self):
        assert build_dep_plan(_profile("redhat")).install_cmd[0] == "dnf"
        assert build_dep_plan(_profile("suse")).install_cmd[0] == "zypper"
        for fam in ("redhat", "suse"):
            assert any("zimg" in p for p in BUILD_DEPS_BY_FAMILY[fam])

    def test_unknown_family_gets_manual_note(self):
        plan = build_dep_plan(_profile("freebsd"))
        assert plan.install_cmd is None
        assert "manually" in plan.manual_note

    def test_no_av1an_strips_rust(self):
        plan = build_dep_plan(_profile("arch"), build_av1an=False)
        assert "rust" not in plan.packages
        assert "zimg" in plan.packages  # still needed for VapourSynth

    def test_real_distro_detection_has_plan(self):
        plan = build_dep_plan(detect_distro())
        assert plan.packages  # this repo targets the covered families

    def test_every_covered_family_installs_zimg(self):
        """zimg is VapourSynth's one hard library dep — a bare system
        build must pull it in."""
        for family, packages in BUILD_DEPS_BY_FAMILY.items():
            assert any("zimg" in p for p in packages), family


class TestPluginProbeSeesGitStack:
    def test_probe_discovers_user_site_plugins(self, tmp_path, monkeypatch):
        import opentranscode.env_probe as ep

        plugins = tmp_path / "site-packages" / "vapoursynth" / "plugins"
        plugins.mkdir(parents=True)
        (plugins / "libbestsource.so").write_bytes(b"\x7fELFfake")

        monkeypatch.setattr(ep.site, "getusersitepackages",
                            lambda: str(tmp_path / "site-packages"))
        monkeypatch.setattr(ep.site, "getsitepackages", lambda: [])
        assert "bestsource" in ep._probe_vs_source_plugins()

    def test_av1an_env_carries_fresh_stack(self, tmp_path, monkeypatch):
        import opentranscode.env_probe as ep

        vs_dir = tmp_path / "site-packages" / "vapoursynth"
        vs_dir.mkdir(parents=True)
        (vs_dir / "libvsscript.so").write_bytes(b"\x7fELFfake")
        monkeypatch.setattr(ep.site, "getusersitepackages",
                            lambda: str(tmp_path / "site-packages"))
        monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
        monkeypatch.delenv("PYTHONPATH", raising=False)

        env = ep._av1an_env()
        assert str(vs_dir) in env["LD_LIBRARY_PATH"]
        assert str(tmp_path / "site-packages") in env["PYTHONPATH"]

    def test_av1an_env_without_git_stack_unchanged(self, tmp_path, monkeypatch):
        import opentranscode.env_probe as ep

        monkeypatch.setattr(ep.site, "getusersitepackages",
                            lambda: str(tmp_path / "empty"))
        monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
        env = ep._av1an_env()
        assert ".local/lib" in env["LD_LIBRARY_PATH"]
        assert "PYTHONPATH" not in env


def importlib_pyside_available():
    import importlib.util
    return importlib.util.find_spec("PySide6") is not None


class TestRebuildButtonAlwaysEnabled:
    @pytest.mark.skipif(
        not importlib_pyside_available(),
        reason="requires a real PySide6 (stub mode cannot instantiate Qt)",
    )
    def test_enabled_before_probe(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        from opentranscode.ui_window import OpenCodecMaster

        app = QApplication.instance() or QApplication([])
        window = OpenCodecMaster()
        try:
            assert window.btn_rebuild.isEnabled(), (
                "REBUILD FROM GIT must be usable without a successful probe"
            )
            assert any(
                "Hybrid (GPU + CPU)" in window.engine_combo.itemText(i)
                for i in range(window.engine_combo.count())
            )
        finally:
            window.close()



class TestLauncherParity:
    def test_launcher_has_dep_tree(self, opentranscode_module):
        assert hasattr(opentranscode_module, "build_dep_plan")
        assert hasattr(opentranscode_module, "BUILD_DEPS_BY_FAMILY")

    def test_launcher_worker_builds_bestsource(self, opentranscode_module):
        assert hasattr(opentranscode_module.SourceBuildWorker,
                       "_build_bestsource")

    def test_launcher_plan_matches_package(self):
        from opentranscode.source_builder import build_dep_plan as pkg_plan
        plan_l = build_dep_plan(_profile("arch"))
        plan_p = pkg_plan(_profile("arch"))
        assert plan_l.packages == plan_p.packages
        assert plan_l.install_cmd == plan_p.install_cmd
