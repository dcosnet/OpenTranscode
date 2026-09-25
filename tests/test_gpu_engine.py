"""v4.6.0 GPU (NVENC) engine tests.

Covers:
  - ``GpuInfo`` defaults and properties
  - ``_probe_gpu``: compiled-in detection + functional smoke test gate
    (the driver/ffmpeg NVENC API mismatch case), with subprocess mocked
  - ``resolve_gpu_encoder``: the auto/gpu/cpu decision matrix
  - codec profile gpu fields + NVENC vargs shape
  - ``FFMPEG_LIB_KEY_MAP`` nvenc entries
  - CLI ``--engine`` flag
  - EncoderWorker engine attribute + env fallback
  - launcher-script parity (resolve_gpu_encoder + engine param exist)
"""
import inspect
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from conftest import capture_signal

from opentranscode.codec_profiles import (
    FFMPEG_LIB_KEY_MAP,
    VIDEO_CODECS,
    _av1_nvenc_args,
    _hevc_nvenc_args,
)
from opentranscode.encoder_worker import EncoderWorker, resolve_gpu_encoder
from opentranscode.env_probe import GpuInfo, _NVENC_ENCODER_NAMES, _probe_gpu


# ─────────────────────────────────────────────────────────────────────────────
#  GpuInfo
# ─────────────────────────────────────────────────────────────────────────────

class TestGpuInfo:
    def test_defaults_have_no_gpu(self):
        info = GpuInfo()
        assert info.has_gpu is False
        assert info.usable_encoders == []
        assert info.first_failure_detail == ""
        assert info.name == ""

    def test_usable_encoders_in_preference_order(self):
        info = GpuInfo()
        for enc in reversed(_NVENC_ENCODER_NAMES):
            info.functional[enc] = True
        assert info.usable_encoders == list(_NVENC_ENCODER_NAMES)

    def test_first_failure_detail_skips_empty(self):
        info = GpuInfo()
        info.details["av1_nvenc"] = ""
        info.details["hevc_nvenc"] = "driver too old"
        assert info.first_failure_detail == "driver too old"


# ─────────────────────────────────────────────────────────────────────────────
#  _probe_gpu (subprocess mocked)
# ─────────────────────────────────────────────────────────────────────────────

def _fake_run_factory(encoders_output: str, smoke_ok: bool):
    """Return a subprocess.run stand-in: -encoders prints encoders_output;
    the smoke test succeeds or fails per smoke_ok."""
    def fake_run(cmd, **kw):
        if "-encoders" in cmd:
            res = MagicMock(returncode=0)
            res.stdout = encoders_output
            return res
        # Smoke test — fail with the real-world driver mismatch text.
        if smoke_ok:
            res = MagicMock(returncode=0)
            res.stderr = ""
            return res
        res = MagicMock(returncode=-22)
        res.stderr = (
            "[hevc_nvenc @ 0x56146c9e2240] Driver does not support the "
            "required nvenc API version. Required: 13.1 Found: 13.0\n"
            "[hevc_nvenc @ 0x56146c9e2240] The minimum required Nvidia "
            "driver for nvenc is 610.00 or newer\n"
        )
        return res
    return fake_run


class TestProbeGpu:
    def test_no_ffmpeg_returns_empty(self):
        assert _probe_gpu(None).has_gpu is False

    def test_no_nvenc_in_build(self, monkeypatch):
        monkeypatch.setattr(
            "opentranscode.env_probe.subprocess.run",
            _fake_run_factory("V..... libsvtav1 SVT-AV1 encoder\n", smoke_ok=True),
        )
        info = _probe_gpu("/usr/bin/ffmpeg")
        assert any(info.encoders.values()) is False
        assert info.has_gpu is False
        assert info.functional == {}

    def test_compiled_in_but_driver_too_old(self, monkeypatch):
        out = (" V....D hevc_nvenc NVIDIA NVENC hevc encoder\n"
               " V....D h264_nvenc NVIDIA NVENC H.264 encoder\n")
        monkeypatch.setattr(
            "opentranscode.env_probe.subprocess.run",
            _fake_run_factory(out, smoke_ok=False),
        )
        info = _probe_gpu("/usr/bin/ffmpeg")
        assert info.encoders["hevc_nvenc"] is True
        assert info.functional["hevc_nvenc"] is False
        assert "API version" in info.first_failure_detail
        assert info.has_gpu is False

    def test_functional_nvenc(self, monkeypatch):
        out = " V....D hevc_nvenc NVIDIA NVENC hevc encoder\n"
        monkeypatch.setattr(
            "opentranscode.env_probe.subprocess.run",
            _fake_run_factory(out, smoke_ok=True),
        )
        monkeypatch.setattr(
            "opentranscode.env_probe.shutil.which", lambda name: None
        )
        info = _probe_gpu("/usr/bin/ffmpeg")
        assert info.has_gpu is True
        assert info.usable_encoders == ["hevc_nvenc"]

    def test_smoke_test_is_live_encode(self, monkeypatch):
        """The functional gate must run an actual encode, not just grep."""
        seen_cmds = []

        def fake_run(cmd, **kw):
            seen_cmds.append(cmd)
            res = MagicMock(returncode=0)
            res.stdout = " V....D hevc_nvenc NVIDIA NVENC hevc encoder\n"
            res.stderr = ""
            return res

        monkeypatch.setattr("opentranscode.env_probe.subprocess.run", fake_run)
        monkeypatch.setattr("opentranscode.env_probe.shutil.which", lambda n: None)
        _probe_gpu("/usr/bin/ffmpeg")
        # Stage 1 (-encoders) + Stage 2 (lavfi source + -c:v hevc_nvenc).
        smoke = [c for c in seen_cmds if "lavfi" in c]
        assert len(smoke) == 1
        assert "hevc_nvenc" in smoke[0]
        assert "-f" in smoke[0] and "null" in smoke[0]


# ─────────────────────────────────────────────────────────────────────────────
#  resolve_gpu_encoder decision matrix
# ─────────────────────────────────────────────────────────────────────────────

class _FakeGpu:
    def __init__(self, functional):
        self.functional = functional
        self.name = "Test GPU"


class _FakeEnv:
    def __init__(self, functional):
        self.gpu = _FakeGpu(functional)
        self.av1an_flags = {}


def _codec_by_label(fragment):
    return next(c for c in VIDEO_CODECS if fragment in c.label)


class TestResolveGpuEncoder:
    def test_auto_uses_functional_gpu(self):
        env = _FakeEnv({"hevc_nvenc": True})
        codec = _codec_by_label("x265")
        assert resolve_gpu_encoder("auto", codec, env) == ("hevc_nvenc", "nvenc")

    def test_auto_falls_back_when_smoke_failed(self):
        # The real GTX 1070 + ffmpeg 9.0.2 case: encoder listed but the
        # live test failed → CPU.
        env = _FakeEnv({"hevc_nvenc": False})
        codec = _codec_by_label("x265")
        assert resolve_gpu_encoder("auto", codec, env) == (None, None)

    def test_engine_cpu_never_gpu(self):
        env = _FakeEnv({"hevc_nvenc": True})
        codec = _codec_by_label("x265")
        assert resolve_gpu_encoder("cpu", codec, env) == (None, None)

    def test_engine_gpu_requires_functional(self):
        env = _FakeEnv({"hevc_nvenc": False})
        codec = _codec_by_label("x265")
        assert resolve_gpu_encoder("gpu", codec, env) == (None, None)
        env2 = _FakeEnv({"hevc_nvenc": True})
        assert resolve_gpu_encoder("gpu", codec, env2) == ("hevc_nvenc", "nvenc")

    def test_vp9_needs_vaapi_profile(self):
        env = _FakeEnv({"av1_nvenc": True, "hevc_nvenc": True, "h264_nvenc": True})
        codec = _codec_by_label("VP9")
        # No profile → legacy nvenc fallback → VP9 has none.
        assert resolve_gpu_encoder("auto", codec, env) == (None, None)

    def test_av1_gpu_only_when_functional(self):
        env = _FakeEnv({"av1_nvenc": False})
        codec = _codec_by_label("AV1")
        assert resolve_gpu_encoder("auto", codec, env) == (None, None)
        env2 = _FakeEnv({"av1_nvenc": True})
        assert resolve_gpu_encoder("auto", codec, env2) == ("av1_nvenc", "nvenc")

    def test_env_without_gpu_attr_is_safe(self):
        class _NoGpu:
            pass
        codec = _codec_by_label("x265")
        assert resolve_gpu_encoder("auto", codec, _NoGpu()) == (None, None)

    # ── v4.8.0: profile-driven resolution ──
    def test_profile_selects_vaapi_encoder(self):
        from opentranscode.gpu_profiles import gpu_profile_by_key
        env = _FakeEnv({"hevc_vaapi": True, "h264_vaapi": True})
        env.av1an_flags = {"gpu_profile": "amd-rdna3"}
        codec = _codec_by_label("x265")
        enc, api = resolve_gpu_encoder("auto", codec, env)
        assert (enc, api) == ("hevc_vaapi", "vaapi")

    def test_profile_respects_functional_gate(self):
        env = _FakeEnv({"hevc_vaapi": False})
        env.av1an_flags = {"gpu_profile": "amd-rdna3"}
        codec = _codec_by_label("x265")
        assert resolve_gpu_encoder("auto", codec, env) == (None, None)

    def test_compute_profile_has_no_encoders(self):
        # CMP 170HX / A100 class: claims nothing → CPU.
        env = _FakeEnv({})
        env.av1an_flags = {"gpu_profile": "nv-compute"}
        codec = _codec_by_label("x265")
        assert resolve_gpu_encoder("auto", codec, env) == (None, None)

    def test_pascal_profile_blocks_av1(self):
        # AV1 on a 1070-class card: profile has no av1 encoder → CPU.
        env = _FakeEnv({"av1_nvenc": True})  # functional (e.g. different card)
        env.av1an_flags = {"gpu_profile": "nv-pascal"}
        codec = _codec_by_label("AV1")
        assert resolve_gpu_encoder("auto", codec, env) == (None, None)

    def test_matched_profile_used_when_no_selection(self):
        from opentranscode.gpu_profiles import gpu_profile_by_key
        env = _FakeEnv({"hevc_qsv": True})
        env.av1an_flags = {}
        env.gpu.profile_key = "intel-arc"
        codec = _codec_by_label("x265")
        assert resolve_gpu_encoder("auto", codec, env) == ("hevc_qsv", "qsv")


# ─────────────────────────────────────────────────────────────────────────────
#  Codec profiles: gpu fields + NVENC vargs
# ─────────────────────────────────────────────────────────────────────────────

class TestCodecProfileGpuFields:
    def test_av1_has_av1_nvenc(self):
        av1 = _codec_by_label("AV1")
        assert av1.gpu_encoder == "av1_nvenc"
        assert av1.gpu_vargs_fn is not None

    def test_hevc_has_hevc_nvenc(self):
        x265 = _codec_by_label("x265")
        assert x265.gpu_encoder == "hevc_nvenc"

    def test_vp9_has_no_gpu_encoder(self):
        vp9 = _codec_by_label("VP9")
        assert vp9.gpu_encoder == ""

    def test_nvenc_vargs_use_cq_mode(self):
        args = _hevc_nvenc_args(28, 7)
        assert args[0] == "-c:v" and args[1] == "hevc_nvenc"
        assert "-cq" in args and "28" in args
        assert "-rc" in args and "vbr" in args
        assert "-b:v" in args and "0" in args
        assert "-pix_fmt" in args and "yuv420p" in args
        av1_args = _av1_nvenc_args(32, 6)
        assert "av1_nvenc" in av1_args

    def test_skip_existing_codec_name_unchanged_by_gpu(self):
        """hevc_nvenc outputs the same ffprobe codec_name ("hevc") as
        libx265, so skip-existing works across engine switches."""
        x265 = _codec_by_label("x265")
        assert x265.ffprobe_codec_name == "hevc"
        av1 = _codec_by_label("AV1")
        assert av1.ffprobe_codec_name == "av1"

    def test_ffmpeg_lib_key_map_has_nvenc(self):
        assert FFMPEG_LIB_KEY_MAP["hevc_nvenc"] == "hevc_nvenc"
        assert FFMPEG_LIB_KEY_MAP["h264_nvenc"] == "h264_nvenc"
        assert FFMPEG_LIB_KEY_MAP["av1_nvenc"] == "av1_nvenc"


# ─────────────────────────────────────────────────────────────────────────────
#  CLI --engine flag
# ─────────────────────────────────────────────────────────────────────────────

class TestCliEngineFlag:
    def test_engine_choices(self):
        from opentranscode.cli import build_parser
        p = build_parser()
        assert p.parse_args(["--engine", "gpu"]).engine == "gpu"
        assert p.parse_args(["--engine", "cpu"]).engine == "cpu"
        assert p.parse_args([]).engine == "auto"

    def test_engine_rejects_unknown(self):
        from opentranscode.cli import build_parser
        p = build_parser()
        with pytest.raises(SystemExit):
            p.parse_args(["--engine", "tape"])


# ─────────────────────────────────────────────────────────────────────────────
#  EncoderWorker engine attribute + GPU encode path
# ─────────────────────────────────────────────────────────────────────────────

class TestEncoderWorkerEngine:
    def _make_worker(self, mock_env, engine=None, use_ffmpeg_fallback=True):
        from opentranscode.codec_profiles import (
            AUDIO_PROFILES, CONTAINER_PROFILES, RESOLUTION_PRESETS,
        )
        return EncoderWorker(
            in_dir=Path("/tmp"),
            out_dir=Path("/tmp"),
            video_codec=_codec_by_label("x265"),
            audio_profile=AUDIO_PROFILES[0],
            container=CONTAINER_PROFILES[0],
            crf=28,
            preset_label="Medium (7)",
            delete_source=False,
            env=mock_env,
            extensions={".mkv"},
            resolution=RESOLUTION_PRESETS[0],
            use_ffmpeg_fallback=use_ffmpeg_fallback,
            engine=engine,
        )

    def test_engine_attr_stored(self, mock_env):
        w = self._make_worker(mock_env, engine="gpu")
        assert w.engine == "gpu"

    def test_engine_falls_back_to_env_flags(self, mock_env):
        mock_env.av1an_flags["engine"] = "cpu"
        try:
            w = self._make_worker(mock_env)  # engine not passed
            assert w.engine == "cpu"
        finally:
            mock_env.av1an_flags.pop("engine", None)

    def test_engine_rejects_unknown_value(self, mock_env):
        mock_env.av1an_flags.pop("engine", None)
        w = self._make_worker(mock_env, engine="quantum")
        assert w.engine == "auto"

    def test_gpu_encoder_unresolved_before_run(self, mock_env):
        w = self._make_worker(mock_env)
        assert w._gpu_encoder is None

    def test_gpu_mode_builds_nvenc_command(self, mock_env):
        """engine=auto + functional hevc_nvenc → the ffmpeg command uses
        hevc_nvenc with -cq instead of libx265 with -crf."""
        mock_env.av1an_flags["verbose"] = True
        mock_env.gpu = _FakeGpu({"hevc_nvenc": True})
        mock_env.ffmpeg_libs["hevc_nvenc"] = True
        w = self._make_worker(mock_env, engine="auto", use_ffmpeg_fallback=False)
        # run() resolves the engine before the encode loop; mirror that
        # here (this test targets the command-construction path only).
        w._gpu_encoder, w._gpu_api = resolve_gpu_encoder(
            w.engine, w.video_codec, mock_env)
        w.use_ffmpeg_fallback = True
        w.resolution = type("R", (), {"width": None, "height": None})()
        w._temp_dir = Path("/tmp")
        w._current_temps = []
        w._file_res_map = {}
        w._stop = False
        w._consecutive_fail_count = 0
        w._last_fail_pattern = None
        w.audio_level_db = 0.0
        w.encode_timeout = 60
        logs: list[str] = []
        w.log_msg = capture_signal(logs)
        w._run_with_stop_check = MagicMock(
            return_value=("ok", 0, "", "")
        )

        src = Path("/tmp/fake_src.mkv")
        out = Path("/tmp/fake_out.mkv")
        ok = w._ffmpeg_fallback_encode(src, src, out)

        assert ok is False  # "output exists" check fails (mock ran nothing)
        cmd = w._run_with_stop_check.call_args[0][0]
        assert "hevc_nvenc" in cmd
        assert "-cq" in cmd
        assert "libx265" not in cmd

    def test_cpu_mode_keeps_libx265(self, mock_env):
        mock_env.gpu = _FakeGpu({"hevc_nvenc": True})
        w = self._make_worker(mock_env, engine="cpu", use_ffmpeg_fallback=True)
        w.resolution = type("R", (), {"width": None, "height": None})()
        w.audio_level_db = 0.0
        w.encode_timeout = 60
        w._run_with_stop_check = MagicMock(return_value=("ok", 0, "", ""))
        src = Path("/tmp/fake_src.mkv")
        w._ffmpeg_fallback_encode(src, src, Path("/tmp/fake_out.mkv"))
        cmd = w._run_with_stop_check.call_args[0][0]
        assert "libx265" in cmd
        assert "hevc_nvenc" not in cmd


# ─────────────────────────────────────────────────────────────────────────────
#  Launcher parity
# ─────────────────────────────────────────────────────────────────────────────

class TestLauncherParity:
    def test_launcher_defines_resolve_gpu_encoder(self, opentranscode_module):
        assert hasattr(opentranscode_module, "resolve_gpu_encoder")

    def test_launcher_worker_accepts_engine(self, opentranscode_module):
        sig = inspect.signature(opentranscode_module.EncoderWorker.__init__)
        assert "engine" in sig.parameters

    def test_launcher_profiles_have_gpu_fields(self, opentranscode_module):
        for vc in opentranscode_module.VIDEO_CODECS:
            assert hasattr(vc, "gpu_encoder")
            assert hasattr(vc, "gpu_vargs_fn")
