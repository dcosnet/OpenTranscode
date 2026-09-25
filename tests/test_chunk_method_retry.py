"""
v7 behavior tests — chunk-method retry on y4m pipe break.

Verifies the v4.0.0 fix for the "works up until near the end, never saves
chunks into a full file" production bug. When av1an's Hybrid chunk method
fails on a phone-recorded MP4 with sparse keyframes, every chunk's
encoder dies with:

    [h264 @ 0x...] error while decoding MB 35 25
    Encoding          Failed to read y4m frame delimiter. Read broken. EOF: 1

The fix retries the file with ``--chunk-method select`` (VapourSynth's
select() filter) before falling back to pure ffmpeg. This is faster than
the ffmpeg fallback (chunk-parallel still works) and produces identical-
quality output (same encoder, same params).

The production log that revealed this bug is at:
    logs/av1an.log.2026-07-13
"""
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from conftest import capture_signal


OPENTRANSCODE_PATH = Path(__file__).resolve().parent.parent / "open-transcode.py"


def _load_opentranscode_module():
    if not OPENTRANSCODE_PATH.exists():
        pytest.skip(f"open-transcode.py not found at {OPENTRANSCODE_PATH}")
    _install_pyside6_stubs()
    spec = importlib.util.spec_from_file_location("open_transcode", str(OPENTRANSCODE_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _install_pyside6_stubs():
    if any(name in sys.modules for name in
           ("PySide6", "PySide6.QtWidgets", "PySide6.QtCore", "PySide6.QtGui")):
        # Check if it's a real PySide6 or our stub. If real, leave it alone.
        if getattr(sys.modules.get("PySide6"), "_otc_stub", False):
            return
        # Real PySide6 — don't install stubs over it
        try:
            importlib.util.find_spec("PySide6.QtCore")
            return
        except (ImportError, ValueError):
            pass

    import types
    pyside6 = types.ModuleType("PySide6")
    pyside6._otc_stub = True
    qt_widgets = types.ModuleType("PySide6.QtWidgets")
    qt_core = types.ModuleType("PySide6.QtCore")
    qt_gui = types.ModuleType("PySide6.QtGui")

    class _QThread:
        def __init__(self, *a, **kw): pass
        def start(self): pass
        def isRunning(self): return False
        def wait(self, ms=None): pass

    class _Signal:
        def __init__(self, *a, **kw): pass
        def connect(self, *a, **kw): pass
        def emit(self, *a, **kw): pass

    def _Slot(*a, **kw):
        def deco(fn): return fn
        return deco

    qt_core.QThread = _QThread
    qt_core.Signal = _Signal
    qt_core.Slot = _Slot
    qt_core.Qt = MagicMock()
    qt_core.QPointF = MagicMock()
    qt_core.QRectF = MagicMock()
    qt_core.QTimer = MagicMock()

    class _QWidget:
        def __init__(self, *a, **kw): pass
    class _QMainWindow(_QWidget): pass
    qt_widgets.QWidget = _QWidget
    qt_widgets.QMainWindow = _QMainWindow
    for name in ("QApplication", "QVBoxLayout", "QHBoxLayout", "QLabel",
                 "QLineEdit", "QPushButton", "QComboBox", "QCheckBox",
                 "QTextEdit", "QFileDialog", "QGroupBox", "QStatusBar",
                 "QMessageBox", "QStyleFactory"):
        setattr(qt_widgets, name, MagicMock())

    for n in ("QFont", "QPalette", "QColor", "QPainter", "QPen", "QBrush",
              "QRadialGradient", "QFontMetrics"):
        setattr(qt_gui, n, MagicMock())

    pyside6.QtWidgets = qt_widgets
    pyside6.QtCore = qt_core
    pyside6.QtGui = qt_gui
    sys.modules["PySide6"] = pyside6
    sys.modules["PySide6.QtWidgets"] = qt_widgets
    sys.modules["PySide6.QtCore"] = qt_core
    sys.modules["PySide6.QtGui"] = qt_gui


@pytest.fixture(scope="module")
def opentranscode_module():
    return _load_opentranscode_module()


# ─────────────────────────────────────────────────────────────────────────────
# Realistic stderr from a y4m-pipe-break failure (extracted from the
# production log at logs/av1an.log.2026-07-13). The key markers are:
#   - "Failed to read y4m frame delimiter" (the broken y4m pipe)
#   - "[h264 @ ...] error while decoding MB" (decoder failing mid-GOP)
#   - "SUMMARY" + "Average Speed" (SVT-AV1's per-chunk summary block,
#     printed because the encoder ran briefly on partial data before the
#     pipe broke — this previously triggered the v6-03 "concat failure"
#     misdiagnosis)
# ─────────────────────────────────────────────────────────────────────────────
_Y4M_BREAK_STDERR = (
    "INFO encode_file: av1an_core::context: Input: 1920x1080 @ 29.763 fps, YUVJ420P, SDR\n"
    "INFO encode_file: av1an_core::scenes: scenecut: found 8 scene(s) "
    "[with extra_splits (298 frames): 16 scene(s)]\n"
    "DEBUG encode_file: av1an_core::context: Segmenting video\n"
    "DEBUG encode_file: av1an_core::context: Segment done\n"
    "INFO encode_file: av1an_core::context: \n"
    "        Encoding          Failed to read y4m frame delimiter. Read broken. EOF: 1\n"
    "        [h264 @ 0x55da365b30c0] error while decoding MB 35 25\n"
    "WARN encode_chunk{worker_id=5 total_chunks=16 chunk_index=\"00011\"}: "
    "av1an_core::broker: Encoder failed (on chunk 11):\n"
    "        Encoding          Failed to read y4m frame delimiter. Read broken. EOF: 1\n"
    "        SUMMARY -----------------------------------------------------------------\n"
    "        Average Speed:\t\t2.501 fps\n"
    "        [h264 @ 0x55da365b30c0] error while decoding MB 35 25\n"
    "ERROR av1an_core::broker: [chunk 4] [chunk 4] encoder failed 3 times, "
    "shutting down worker: encoder crashed: exit status: 0\n"
)


class TestY4mBreakPatternInErrorTable:
    """v4.0.0: 'Failed to read y4m frame delimiter' is in the error_patterns table."""

    def test_y4m_break_pattern_in_source(self, opentranscode_module):
        """The error_patterns table should include the y4m break pattern."""
        src = Path(OPENTRANSCODE_PATH).read_text()
        assert "Failed to read y4m frame delimiter" in src, \
            "error_patterns table should include 'Failed to read y4m frame delimiter'"

    def test_v7_01_retry_logic_in_source(self, opentranscode_module):
        """The v4.0.0 retry-with-select logic should be present in _encode_one."""
        src = Path(OPENTRANSCODE_PATH).read_text()
        assert 'chunk_method="select"' in src, \
            "_encode_one should have a retry path that passes chunk_method='select'"
        assert 'chunk_method_override' in src, \
            "_encode_one should cache the working chunk_method in chunk_method_override"

    def test_v6_03_diagnostic_guarded_by_y4m_check(self, opentranscode_module):
        """v4.0.0: the v6-03 SUMMARY-block concat-failure diagnostic must NOT
        fire when the y4m break marker is present (otherwise it misdiagnoses
        chunk-extraction failures as concat failures)."""
        src = Path(OPENTRANSCODE_PATH).read_text()
        # Find the v6-03 SUMMARY block check and verify it's guarded by
        # the y4m break exclusion.
        assert '"Failed to read y4m frame delimiter" not in stderr_full' in src, \
            "v6-03 SUMMARY block diagnostic must be guarded by y4m break exclusion"

    def test_vs_plugin_probe_in_source(self, opentranscode_module):
        """v4.0.0: env_probe should include the VapourSynth source plugin probe."""
        src = Path(OPENTRANSCODE_PATH).read_text()
        assert "_probe_vs_source_plugins" in src, \
            "env_probe should include _probe_vs_source_plugins helper"
        assert "_VS_PLUGIN_PROBE_PATHS" in src, \
            "env_probe should include the VS plugin path table"


class TestChunkMethodParameter:
    """v4.0.0: _encode_one accepts a chunk_method parameter for retries."""

    def test_encode_one_accepts_chunk_method_kwarg(self, opentranscode_module, tmp_path):
        """_encode_one should accept chunk_method as a keyword argument."""
        import inspect
        sig = inspect.signature(opentranscode_module.EncoderWorker._encode_one)
        params = list(sig.parameters.keys())
        assert "chunk_method" in params, \
            f"_encode_one should accept chunk_method parameter; got params: {params}"
        # Default should be None (no override)
        assert sig.parameters["chunk_method"].default is None, \
            "chunk_method default should be None"


class TestY4mBreakRetry:
    """v4.0.0: When av1an fails with the y4m break pattern, retry with select."""

    def test_y4m_break_triggers_select_retry(self, opentranscode_module, tmp_path):
        """When av1an fails with the y4m break pattern (and we're not already
        using select), the code should retry with --chunk-method select."""
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            pytest.skip("ffmpeg not available")

        # Create a real test video
        test_video = tmp_path / "input.mp4"
        subprocess.run(
            [ffmpeg_bin, "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=24",
             "-c:v", "libx264", "-y", str(test_video)],
            capture_output=True, timeout=15,
        )
        if not test_video.exists():
            pytest.skip("Could not generate test video")

        av1_codec = next(c for c in opentranscode_module.VIDEO_CODECS if c.label == "AV1 (SVT-AV1)")
        opus_audio = next(a for a in opentranscode_module.AUDIO_PROFILES if a.label == "Opus (96k)")
        mkv_container = next(c for c in opentranscode_module.CONTAINER_PROFILES if c.ext == "mkv")
        original_res = next(r for r in opentranscode_module.RESOLUTION_PRESETS if r.category == "original")

        env = opentranscode_module.probe_environment()
        env.av1an_path = "/usr/bin/av1an"
        # Start with NO chunk_method_override — av1an auto-selects Hybrid
        env.av1an_flags = {
            "worker": "--workers",
            "video_params": "--video-params",
            "audio_params": "--audio-params",
            "svt_name": "svt-av1",
            "concat_method": "ffmpeg",
            "has_chunk_method": True,
            # NOTE: chunk_method_override intentionally absent — simulates
            # the production scenario where env_probe didn't pre-set it
        }

        worker = opentranscode_module.EncoderWorker(
            in_dir=tmp_path,
            out_dir=tmp_path / "output",
            video_codec=av1_codec,
            audio_profile=opus_audio,
            container=mkv_container,
            crf=32,
            preset_label="Fast (4)",
            delete_source=False,
            env=env,
            extensions={".mp4"},
            resolution=original_res,
            use_ffmpeg_fallback=False,
        )

        # Track which chunk_method each call used
        call_chunk_methods: list[str | None] = []

        def mock_run(cmd, **kw):
            # Extract the chunk method from the command
            chunk_m = None
            if "--chunk-method" in cmd:
                idx = cmd.index("--chunk-method")
                chunk_m = cmd[idx + 1] if idx + 1 < len(cmd) else None
            call_chunk_methods.append(chunk_m)

            if chunk_m is None or chunk_m == "hybrid":
                # First attempt (auto/Hybrid) — fail with y4m break
                return ("ok", 1, "", _Y4M_BREAK_STDERR)
            else:
                # Retry with select — succeed by producing a fake output
                # (the actual encode is mocked; we just create the file)
                # Find the output path (last arg of -o)
                if "-o" in cmd:
                    out_idx = cmd.index("-o")
                    out_path = Path(cmd[out_idx + 1])
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    out_path.write_bytes(b"\x00" * 8192)  # 8KB fake AV1 output
                return ("ok", 0, "", "encoding finished")

        worker._run_with_stop_check = mock_run

        # Mock _ffmpeg_fallback_encode — should NOT be called (select retry succeeds first)
        worker._ffmpeg_fallback_encode = MagicMock(return_value=True)

        logs: list[str] = []
        worker.log_msg = capture_signal(logs)
        # v4.2.1+: RETRY log lines are verbose-only; the test asserts on
        # them, so enable verbose output.
        worker.verbose = True

        # Setup required attributes
        worker._current_temps = []
        worker._file_res_map = {}
        worker._stop = False
        worker._consecutive_fail_count = 0
        worker._last_fail_pattern = None

        output_f = tmp_path / "output" / "input_archived.mkv"
        result = worker._encode_one(test_video, test_video, output_f, 1)

        # Should have succeeded via the select retry
        assert result is True, f"Expected retry to succeed. Logs: {logs}"

        # Should have been called twice: once with no chunk_method (auto),
        # once with chunk_method="select"
        assert len(call_chunk_methods) == 2, \
            f"Expected 2 calls (Hybrid fail + select retry), got {len(call_chunk_methods)}: {call_chunk_methods}"
        assert call_chunk_methods[0] is None, \
            f"First call should have no chunk_method (auto/Hybrid), got: {call_chunk_methods[0]}"
        assert call_chunk_methods[1] == "select", \
            f"Second call should use --chunk-method select, got: {call_chunk_methods[1]}"

        # Should have logged the RETRY message
        assert any("RETRY" in l and "select" in l for l in logs), \
            f"Expected RETRY message with 'select' in logs: {logs}"

        # Should NOT have called ffmpeg fallback (select retry succeeded)
        worker._ffmpeg_fallback_encode.assert_not_called()

        # fail_count should NOT be incremented (retry succeeded)
        assert worker.fail_count == 0, \
            f"fail_count should be 0 after successful select retry, got {worker.fail_count}"

        # v4.0.0: the working chunk_method should be cached for subsequent files
        assert env.av1an_flags.get("chunk_method_override") == "select", \
            f"chunk_method_override should be cached as 'select' for subsequent files"

    def test_y4m_break_no_retry_when_already_select(self, opentranscode_module, tmp_path):
        """When av1an fails with y4m break AND we're already using select,
        don't retry with select again (would infinite-loop). Fall through to
        the v6-01 ffmpeg fallback instead."""
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            pytest.skip("ffmpeg not available")

        test_video = tmp_path / "input.mp4"
        test_video.write_bytes(b"\x00" * 1024)

        av1_codec = next(c for c in opentranscode_module.VIDEO_CODECS if c.label == "AV1 (SVT-AV1)")
        opus_audio = next(a for a in opentranscode_module.AUDIO_PROFILES if a.label == "Opus (96k)")
        mkv_container = next(c for c in opentranscode_module.CONTAINER_PROFILES if c.ext == "mkv")
        original_res = next(r for r in opentranscode_module.RESOLUTION_PRESETS if r.category == "original")

        env = opentranscode_module.probe_environment()
        env.av1an_path = "/usr/bin/av1an"
        # Already forcing select — simulates the case where the user
        # passed --chunk-method select but it still failed (rare, but
        # possible if VapourSynth itself is broken)
        env.av1an_flags = {
            "worker": "--workers",
            "video_params": "--video-params",
            "audio_params": "--audio-params",
            "svt_name": "svt-av1",
            "concat_method": "ffmpeg",
            "has_chunk_method": True,
            "chunk_method_override": "select",
        }

        worker = opentranscode_module.EncoderWorker(
            in_dir=tmp_path,
            out_dir=tmp_path / "output",
            video_codec=av1_codec,
            audio_profile=opus_audio,
            container=mkv_container,
            crf=32,
            preset_label="Fast (4)",
            delete_source=False,
            env=env,
            extensions={".mp4"},
            resolution=original_res,
            use_ffmpeg_fallback=False,
        )

        call_count = [0]
        def mock_run(cmd, **kw):
            call_count[0] += 1
            # Always fail with y4m break (even on select retry)
            return ("ok", 1, "", _Y4M_BREAK_STDERR)
        worker._run_with_stop_check = mock_run

        # Mock ffmpeg fallback to succeed
        def mock_ffmpeg_fallback(file_path, encode_input, output_f):
            output_f.parent.mkdir(parents=True, exist_ok=True)
            output_f.write_bytes(b"\x00" * 8192)
            return True
        worker._ffmpeg_fallback_encode = mock_ffmpeg_fallback

        logs: list[str] = []
        worker.log_msg = capture_signal(logs)
        worker._current_temps = []
        worker._stop = False

        output_f = tmp_path / "output" / "input_archived.mkv"
        result = worker._encode_one(test_video, test_video, output_f, 1)

        # Should have succeeded via ffmpeg fallback (not select retry)
        assert result is True, f"Expected ffmpeg fallback to succeed. Logs: {logs}"

        # Should have been called only ONCE (no select retry since already select)
        assert call_count[0] == 1, \
            f"Expected 1 av1an call (no retry since already select), got {call_count[0]}"

        # Should NOT have logged the select RETRY message
        assert not any("RETRY" in l and "select" in l for l in logs), \
            f"Should not log select RETRY when already using select: {logs}"

        # Should have logged the ffmpeg fallback RETRY
        assert any("RETRY" in l and "ffmpeg fallback" in l for l in logs), \
            f"Expected ffmpeg fallback RETRY in logs: {logs}"

    def test_y4m_break_diagnosis_not_misdiagnosed_as_concat(self, opentranscode_module, tmp_path):
        """v4.0.0: the v6-03 'concat failure' diagnostic must NOT fire when the
        y4m break marker is present. The y4m break pattern's own diagnosis
        should be emitted instead."""
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            pytest.skip("ffmpeg not available")

        test_video = tmp_path / "input.mp4"
        test_video.write_bytes(b"\x00" * 1024)

        av1_codec = next(c for c in opentranscode_module.VIDEO_CODECS if c.label == "AV1 (SVT-AV1)")
        opus_audio = next(a for a in opentranscode_module.AUDIO_PROFILES if a.label == "Opus (96k)")
        mkv_container = next(c for c in opentranscode_module.CONTAINER_PROFILES if c.ext == "mkv")
        original_res = next(r for r in opentranscode_module.RESOLUTION_PRESETS if r.category == "original")

        env = opentranscode_module.probe_environment()
        env.av1an_path = "/usr/bin/av1an"
        # Force select so the retry doesn't happen (we just want to test
        # the diagnostic message)
        env.av1an_flags = {
            "worker": "--workers",
            "video_params": "--video-params",
            "audio_params": "--audio-params",
            "svt_name": "svt-av1",
            "concat_method": "ffmpeg",
            "has_chunk_method": True,
            "chunk_method_override": "select",
        }

        worker = opentranscode_module.EncoderWorker(
            in_dir=tmp_path,
            out_dir=tmp_path / "output",
            video_codec=av1_codec,
            audio_profile=opus_audio,
            container=mkv_container,
            crf=32,
            preset_label="Fast (4)",
            delete_source=False,
            env=env,
            extensions={".mp4"},
            resolution=original_res,
            use_ffmpeg_fallback=False,
        )

        def mock_run(cmd, **kw):
            # Fail with y4m break (which includes SUMMARY + Average Speed)
            return ("ok", 1, "", _Y4M_BREAK_STDERR)
        worker._run_with_stop_check = mock_run

        # Mock ffmpeg fallback to succeed
        worker._ffmpeg_fallback_encode = MagicMock(return_value=True)

        logs: list[str] = []
        worker.log_msg = capture_signal(logs)
        worker._current_temps = []
        worker._stop = False

        output_f = tmp_path / "output" / "input_archived.mkv"
        worker._encode_one(test_video, test_video, output_f, 1)

        # Should have emitted the y4m break diagnosis
        y4m_diagnosis = any(
            "y4m" in l.lower() and "DIAGNOSIS" in l for l in logs
        )
        assert y4m_diagnosis, \
            f"Should emit y4m break DIAGNOSIS. Logs: {logs}"

        # Should NOT have emitted the v6-03 concat failure diagnosis.
        # The v6-03 message starts with "SVT-AV1 encoder completed successfully
        # (SUMMARY block found in stderr)" — check for that unique prefix
        # to avoid matching the y4m break diagnosis which itself says "this
        # is NOT a concat failure".
        concat_diagnosis = any(
            "SVT-AV1 encoder completed successfully" in l
            or "post-encode merge step crashed" in l
            for l in logs
        )
        assert not concat_diagnosis, \
            f"Should NOT emit v6-03 concat failure diagnosis for y4m break. Logs: {logs}"


class TestVSPluginProbe:
    """v4.0.0: _probe_vs_source_plugins detects installed VS plugins."""

    def test_probe_returns_empty_when_no_plugins(self, opentranscode_module, tmp_path, monkeypatch):
        """When no VS plugin .so files exist in any search dir, the probe
        should return an empty list."""
        # Point HOME at an empty tmp dir so the home-dir search paths
        # don't accidentally find real plugins. v4.7.1: the probe also
        # scans the python site-packages plugin dirs — patch those too,
        # or a machine with a git-built BestSource (user site) would
        # legitimately report it and break the hermetic expectation.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
        import opentranscode.env_probe as _ep
        monkeypatch.setattr(_ep.site, "getusersitepackages",
                            lambda: str(tmp_path / "site-packages"))
        monkeypatch.setattr(_ep.site, "getsitepackages", lambda: [])

        result = opentranscode_module._probe_vs_source_plugins()
        assert result == [], \
            f"Expected empty list when no plugins installed, got: {result}"

    def test_probe_detects_lsmash(self, opentranscode_module, tmp_path, monkeypatch):
        """When libvslsmashsource.so is in a search dir, the probe should
        return ['lsmash']."""
        # Create a fake VS plugin dir with a fake lsmash .so.
        # The probe searches ~/.local/lib/vapoursynth/ so we create it there.
        vs_dir = tmp_path / ".local" / "lib" / "vapoursynth"
        vs_dir.mkdir(parents=True)
        (vs_dir / "libvslsmashsource.so").write_bytes(b"\x00" * 16)

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))

        result = opentranscode_module._probe_vs_source_plugins()
        assert "lsmash" in result, \
            f"Expected 'lsmash' in probe result, got: {result}"

    def test_probe_detects_multiple_plugins(self, opentranscode_module, tmp_path, monkeypatch):
        """When multiple plugins are installed, the probe should find them all."""
        vs_dir = tmp_path / ".local" / "lib" / "vapoursynth"
        vs_dir.mkdir(parents=True)
        (vs_dir / "libvslsmashsource.so").write_bytes(b"\x00" * 16)
        (vs_dir / "libffms2.so").write_bytes(b"\x00" * 16)
        (vs_dir / "libbestsource.so").write_bytes(b"\x00" * 16)

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))

        result = opentranscode_module._probe_vs_source_plugins()
        assert "lsmash" in result
        assert "ffms2" in result
        assert "bestsource" in result
        assert len(result) == 3, f"Expected 3 plugins, got: {result}"


class TestCLIChunkMethodFlag:
    """v4.0.0: --chunk-method CLI flag is parsed and validated."""

    def test_chunk_method_flag_accepted(self, opentranscode_module):
        """--chunk-method should be accepted by the CLI parser."""
        parser = opentranscode_module.build_parser() if hasattr(opentranscode_module, "build_parser") else None
        if parser is None:
            # The launcher script doesn't have build_parser; test the package's
            # cli module instead
            from opentranscode.cli import build_parser
            parser = build_parser()

        # Valid values
        for method in ("auto", "select", "hybrid", "segment",
                       "ffms2", "lsmash", "bestsource", "dgdecnv"):
            args = parser.parse_args(["--chunk-method", method])
            assert args.chunk_method == method, \
                f"--chunk-method {method} should parse to {method}"

    def test_chunk_method_rejects_invalid_value(self, opentranscode_module):
        """Invalid --chunk-method values should be rejected by argparse."""
        try:
            from opentranscode.cli import build_parser
        except ImportError:
            pytest.skip("opentranscode.cli not importable (PySide6 missing)")

        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--chunk-method", "invalid-method"])
