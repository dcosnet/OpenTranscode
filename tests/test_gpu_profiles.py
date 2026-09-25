"""v4.8.0 GPU capability-profile tests.

Covers the combined generation dropdown entries: profile data
integrity, the auto-match matrix (including Tesla/data-center and the
crypto-era CMP oddballs), profile-driven encoder resolution, and the
rebuild dep-tree extension.
"""
import os

import pytest

from opentranscode.codec_profiles import VIDEO_CODECS
from opentranscode.encoder_worker import resolve_gpu_encoder
from opentranscode.gpu_profiles import (
    GPU_PROFILES,
    encoder_filter_chain,
    encoder_quality_args,
    encoder_pre_args,
    gpu_profile_by_key,
    match_gpu_profile,
)


class TestProfileData:
    def test_keys_unique(self):
        keys = [p.key for p in GPU_PROFILES]
        assert len(keys) == len(set(keys))

    def test_labels_unique(self):
        labels = [p.label for p in GPU_PROFILES]
        assert len(labels) == len(set(labels))

    def test_cpu_only_profile_claims_nothing(self):
        cpu = gpu_profile_by_key("cpu")
        assert cpu.encoders == {}
        assert cpu.api == "none"

    def test_compute_boards_claim_nothing(self):
        """V100/A100/H100-class boards and CMP 170HX have no NVENC —
        selecting them must land on the CPU path, not crash."""
        compute = gpu_profile_by_key("nv-compute")
        assert compute.encoders == {}

    def test_ada_has_av1_older_nvidia_does_not(self):
        ada = gpu_profile_by_key("nv-ada")
        assert ada.encoders["av1"] == "av1_nvenc"
        for key in ("nv-pascal", "nv-turing", "nv-ampere"):
            assert "av1" not in gpu_profile_by_key(key).encoders

    def test_intel_arc_has_av1_qsv(self):
        arc = gpu_profile_by_key("intel-arc")
        assert arc.api == "qsv"
        assert arc.encoders["av1"] == "av1_qsv"

    def test_amd_rdna3_has_av1_vaapi_rdna12_does_not(self):
        assert gpu_profile_by_key("amd-rdna3").encoders["av1"] == "av1_vaapi"
        assert "av1" not in gpu_profile_by_key("amd-rdna12").encoders

    def test_vp9_only_via_vaapi(self):
        from opentranscode.codec_profiles import VIDEO_CODECS
        vp9 = next(c for c in VIDEO_CODECS if "VP9" in c.label)
        assert vp9.gpu_encoders_by_api == {"vaapi": "vp9_vaapi"}


class TestMatchMatrix:
    @pytest.mark.parametrize("name,expected_key", [
        ("NVIDIA GeForce GTX 1070", "nv-pascal"),
        ("NVIDIA GeForce GTX 1080 Ti", "nv-pascal"),
        ("NVIDIA GeForce RTX 2060", "nv-turing"),
        ("NVIDIA GeForce GTX 1660 SUPER", "nv-turing"),
        ("NVIDIA GeForce RTX 3060", "nv-ampere"),
        ("NVIDIA GeForce RTX 4090", "nv-ada"),
        ("NVIDIA GeForce RTX 5080", "nv-ada"),
        ("Tesla P40", "nv-pascal"),
        ("Tesla T4", "nv-turing"),
        ("NVIDIA A10", "nv-ampere"),
        ("NVIDIA CMP 50HX", "nv-turing"),
        ("NVIDIA CMP 90HX", "nv-ampere"),
        ("NVIDIA CMP 170HX GA100", "nv-compute"),
        ("NVIDIA CMP 170HX", "nv-compute"),
        ("NVIDIA A100-SXM4-40GB", "nv-compute"),
        ("NVIDIA H100", "nv-compute"),
        ("Intel(R) Arc(TM) B580", "intel-arc"),
        ("Intel(R) UHD Graphics 770", "intel-xe"),
    ])
    def test_match(self, name, expected_key):
        assert match_gpu_profile(name).key == expected_key

    def test_no_match_returns_none(self):
        assert match_gpu_profile("") is None
        assert match_gpu_profile("Unknown Widget Graphics") is None


class TestProfileResolution:
    def _env(self, functional, flags=None, profile_key=""):
        class G:
            pass
        g = G()
        g.functional = functional
        g.profile_key = profile_key

        class E:
            pass
        e = E()
        e.gpu = g
        e.av1an_flags = flags or {}
        return e

    def test_hevc_via_pascal_profile(self):
        env = self._env({"hevc_nvenc": True}, {"gpu_profile": "nv-pascal"})
        codec = next(c for c in VIDEO_CODECS if "x265" in c.label)
        assert resolve_gpu_encoder("auto", codec, env) == ("hevc_nvenc", "nvenc")

    def test_arc_qsv_hevc(self):
        env = self._env({"hevc_qsv": True}, {"gpu_profile": "intel-arc"})
        codec = next(c for c in VIDEO_CODECS if "x265" in c.label)
        assert resolve_gpu_encoder("auto", codec, env) == ("hevc_qsv", "qsv")

    def test_vp9_on_amd_stays_cpu(self):
        # AMD VCN has no VP9 encode — even though ffmpeg has vp9_vaapi,
        # the RDNA 3 profile truthfully claims none.
        env = self._env({"vp9_vaapi": True}, {"gpu_profile": "amd-rdna3"})
        codec = next(c for c in VIDEO_CODECS if "VP9" in c.label)
        assert resolve_gpu_encoder("auto", codec, env) == (None, None)

    def test_vaapi_encoder_args_shape(self):
        profile = gpu_profile_by_key("amd-rdna3")
        assert encoder_pre_args(profile)[0] == "-vaapi_device"
        assert "format=nv12,hwupload" in encoder_filter_chain(profile)
        args = encoder_quality_args("vaapi", "hevc_vaapi", 28, 9)
        assert args[:2] == ["-rc_mode", "CQP"]


class TestRebuildGpuDeps:
    def test_nvenc_needs_codec_headers(self):
        from opentranscode.source_builder import gpu_dep_packages
        pkgs = gpu_dep_packages("nvenc", "arch")
        assert "nv-codec-headers" in pkgs

    def test_vaapi_qsv_driver_packages(self):
        from opentranscode.source_builder import gpu_dep_packages
        assert any("libva" in p for p in gpu_dep_packages("vaapi", "arch"))
        assert any("intel-media-driver" in p for p in gpu_dep_packages("qsv", "arch"))

    def test_unpackaged_family_returns_empty(self):
        from opentranscode.source_builder import gpu_dep_packages
        assert gpu_dep_packages("nvenc", "nixos") == []


def importlib_pyside_available():
    import importlib.util
    return importlib.util.find_spec("PySide6") is not None


class TestGpuComboEngineInteraction:
    @pytest.mark.skipif(
        not importlib_pyside_available(),
        reason="requires a real PySide6 (stub mode cannot instantiate Qt)",
    )
    def test_cpu_engine_disables_gpu_menu(self):
        """v4.8.1: ENGINE = CPU greys out the GPU dropdown; the explicit
        'None (CPU-only encode)' entry remains for opting out while a
        GPU-capable engine is selected."""
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        from opentranscode.ui_window import OpenCodecMaster

        app = QApplication.instance() or QApplication([])
        window = OpenCodecMaster()
        try:
            gpu_idx = next(i for i in range(window.gpu_combo.count())
                           if "None (CPU-only" in window.gpu_combo.itemText(i))
            assert gpu_idx > 0, "None (CPU-only) must be an explicit option"

            window.engine_combo.setCurrentIndex(0)  # Auto
            assert window.gpu_combo.isEnabled()
            window.engine_combo.setCurrentIndex(2)  # CPU
            assert not window.gpu_combo.isEnabled(), (
                "ENGINE = CPU must disable the GPU dropdown"
            )
            window.engine_combo.setCurrentIndex(1)  # GPU
            assert window.gpu_combo.isEnabled()
        finally:
            window.close()


class TestLauncherParity:
    def test_launcher_has_gpu_profiles(self, opentranscode_module):
        assert hasattr(opentranscode_module, "GPU_PROFILES")
        assert hasattr(opentranscode_module, "match_gpu_profile")

    def test_launcher_match_same_as_package(self, opentranscode_module):
        from opentranscode.gpu_profiles import match_gpu_profile as pkg_match
        assert opentranscode_module.match_gpu_profile("RTX 4090").key \
            == pkg_match("RTX 4090").key

    def test_launcher_worker_gpu_param(self, opentranscode_module):
        import inspect
        sig = inspect.signature(
            opentranscode_module.SourceBuildWorker.__init__)
        assert "gpu_profile_key" in sig.parameters
