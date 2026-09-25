"""
v6 behavior tests — per-file av1an→ffmpeg fallback.

Verifies that when av1an fails for a specific file (concat failure,
scene-detection panic, or other per-file issue), the encoder automatically
retries with the ffmpeg fallback path.
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
        return
    import types
    pyside6 = types.ModuleType("PySide6")
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


class TestCanFfmpegFallback:
    """v6-01: _can_ffmpeg_fallback checks if ffmpeg has the encoder."""

    def test_returns_true_when_ffmpeg_has_encoder(self, opentranscode_module):
        """When ffmpeg_libs has the encoder, _can_ffmpeg_fallback returns True."""
        worker = opentranscode_module.EncoderWorker.__new__(opentranscode_module.EncoderWorker)
        worker.video_codec = MagicMock()
        worker.video_codec.ffmpeg_encoder = "libsvtav1"
        worker.env = MagicMock()
        worker.env.ffmpeg_libs = {"libsvtav1": True, "libx265": True}

        assert worker._can_ffmpeg_fallback() is True

    def test_returns_false_when_ffmpeg_lacks_encoder(self, opentranscode_module):
        """When ffmpeg_libs does NOT have the encoder, returns False."""
        worker = opentranscode_module.EncoderWorker.__new__(opentranscode_module.EncoderWorker)
        worker.video_codec = MagicMock()
        worker.video_codec.ffmpeg_encoder = "libsvtav1"
        worker.env = MagicMock()
        worker.env.ffmpeg_libs = {"libsvtav1": False, "libx265": True}

        assert worker._can_ffmpeg_fallback() is False


class TestErrorPatternsIncludeSplitScores:
    """v6-02: 'split scores is not empty' panic is in the error_patterns table."""

    def test_split_scores_pattern_in_source(self, opentranscode_module):
        """The error_patterns table should include the 'split scores' pattern."""
        src = Path(OPENTRANSCODE_PATH).read_text()
        assert "split scores is not empty" in src, \
            "error_patterns table should include 'split scores is not empty'"

    def test_summary_detection_in_source(self, opentranscode_module):
        """v6-03: SUMMARY + Average Speed detection for concat failures."""
        src = Path(OPENTRANSCODE_PATH).read_text()
        assert "SUMMARY" in src and "Average Speed" in src, \
            "Should detect encoder SUMMARY block for concat failure diagnosis"


class TestPerFileFallbackRetry:
    """v6-01: When av1an fails, retry with ffmpeg fallback."""

    def test_av1an_failure_triggers_ffmpeg_retry(self, opentranscode_module, tmp_path):
        """When av1an fails (non-systematic) and ffmpeg has the encoder,
        the code should retry with _ffmpeg_fallback_encode."""
        # Create a real test video
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            pytest.skip("ffmpeg not available")

        test_video = tmp_path / "input.mp4"
        subprocess.run(
            [ffmpeg_bin, "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=24",
             "-c:v", "libx264", "-y", str(test_video)],
            capture_output=True, timeout=15,
        )
        if not test_video.exists():
            pytest.skip("Could not generate test video")

        # Build a worker in av1an mode (not ffmpeg fallback)
        av1_codec = next(c for c in opentranscode_module.VIDEO_CODECS if c.label == "AV1 (SVT-AV1)")
        opus_audio = next(a for a in opentranscode_module.AUDIO_PROFILES if a.label == "Opus (96k)")
        mkv_container = next(c for c in opentranscode_module.CONTAINER_PROFILES if c.ext == "mkv")
        original_res = next(r for r in opentranscode_module.RESOLUTION_PRESETS if r.category == "original")

        env = opentranscode_module.probe_environment()
        # Set fake av1an path + flags so the command builder doesn't crash
        env.av1an_path = "/usr/bin/av1an"
        env.av1an_flags = {
            "worker": "--workers",
            "video_params": "--video-params",
            "audio_params": "--audio-params",
            "svt_name": "svt-av1",
            "concat_method": "ffmpeg",
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
            use_ffmpeg_fallback=False,  # av1an mode — will fail and retry
        )

        # Mock _run_with_stop_check to simulate av1an failure
        # (return a SUMMARY block in stderr + non-zero exit code = concat failure)
        def mock_run(cmd, **kw):
            # Simulate av1an encoding all frames then failing at concat
            fake_stderr = (
                "Encoding: 24/24 Frames @ 3.52 fps\n"
                "SUMMARY -----------------------------------------\n"
                "Total Frames\t\tFrame Rate\t\tByte Count\n"
                "          24\t\t23.98 fps\t\t   12345\n\n"
                "Average Speed:\t\t3.598 fps\n"
                "SvtMalloc[info]: you have no memory leak\n\n"
                "source pipe stderr:\n\n"
                "ffmpeg pipe stderr:\n\n"
            )
            return ("ok", 1, "", fake_stderr)

        worker._run_with_stop_check = mock_run

        # Mock _ffmpeg_fallback_encode to simulate success
        def mock_ffmpeg_fallback(file_path, encode_input, output_f):
            # Create the output file so the verify step passes
            output_f.parent.mkdir(parents=True, exist_ok=True)
            output_f.write_bytes(b"\x00" * 1024)  # 1KB fake output
            return True

        worker._ffmpeg_fallback_encode = mock_ffmpeg_fallback

        logs = []
        worker.log_msg = capture_signal(logs)

        # Set up _current_temps and _file_res_map (needed by _process_one_file)
        worker._current_temps = []
        worker._file_res_map = {}
        worker._stop = False
        worker._consecutive_fail_count = 0
        worker._last_fail_pattern = None
        # v4.4.2: enable verbose so _vlog messages (RETRY, RETRY OK) appear
        # in the logs list this test asserts against.
        worker.verbose = True

        # Call _encode_one directly
        output_f = tmp_path / "output" / "input_archived.mkv"
        result = worker._encode_one(test_video, test_video, output_f, 1)

        # Should have retried with ffmpeg and succeeded
        assert result is True, f"Expected retry to succeed. Logs: {logs}"

        # Should have logged the RETRY message
        assert any("RETRY" in l and "ffmpeg fallback" in l for l in logs), \
            f"Expected RETRY message in logs: {logs}"

        # Should have logged RETRY OK
        assert any("RETRY OK" in l for l in logs), \
            f"Expected RETRY OK in logs: {logs}"

        # fail_count should NOT be incremented (retry succeeded)
        assert worker.fail_count == 0, \
            f"fail_count should be 0 after successful retry, got {worker.fail_count}"

    def test_av1an_failure_no_retry_when_ffmpeg_lacks_encoder(self, opentranscode_module, tmp_path):
        """When av1an fails and ffmpeg does NOT have the encoder,
        no retry should happen — just increment fail_count and return False."""
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            pytest.skip("ffmpeg not available")

        test_video = tmp_path / "input.mp4"
        test_video.write_bytes(b"\x00" * 1024)  # fake video

        av1_codec = next(c for c in opentranscode_module.VIDEO_CODECS if c.label == "AV1 (SVT-AV1)")
        opus_audio = next(a for a in opentranscode_module.AUDIO_PROFILES if a.label == "Opus (96k)")
        mkv_container = next(c for c in opentranscode_module.CONTAINER_PROFILES if c.ext == "mkv")
        original_res = next(r for r in opentranscode_module.RESOLUTION_PRESETS if r.category == "original")

        # Build env with libsvtav1=False — can't fallback
        env = opentranscode_module.probe_environment()
        env.av1an_path = "/usr/bin/av1an"
        env.av1an_flags = {
            "worker": "--workers",
            "video_params": "--video-params",
            "audio_params": "--audio-params",
            "svt_name": "svt-av1",
            "concat_method": "ffmpeg",
        }
        env.ffmpeg_libs["libsvtav1"] = False

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

        # Mock av1an failure
        def mock_run(cmd, **kw):
            return ("ok", 1, "", "some av1an error")
        worker._run_with_stop_check = mock_run

        # Mock _ffmpeg_fallback_encode — should NOT be called
        worker._ffmpeg_fallback_encode = MagicMock(return_value=True)

        logs = []
        worker.log_msg = capture_signal(logs)
        worker._current_temps = []
        worker._stop = False

        output_f = tmp_path / "output" / "input_archived.mkv"
        result = worker._encode_one(test_video, test_video, output_f, 1)

        # Should fail (no retry possible)
        assert result is False
        assert worker.fail_count == 1
        # _ffmpeg_fallback_encode should NOT have been called
        worker._ffmpeg_fallback_encode.assert_not_called()
        # Should NOT have logged RETRY
        assert not any("RETRY" in l for l in logs)

    def test_systematic_failure_does_not_retry(self, opentranscode_module, tmp_path):
        """When av1an fails with a systematic issue (VSScript API),
        self._stop is set and no retry should happen."""
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
        env.av1an_flags = {
            "worker": "--workers",
            "video_params": "--video-params",
            "audio_params": "--audio-params",
            "svt_name": "svt-av1",
            "concat_method": "ffmpeg",
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

        # Mock av1an VSScript failure — this sets self._stop = True
        def mock_run(cmd, **kw):
            return ("ok", 1, "", "Failed to get VSScript API")
        worker._run_with_stop_check = mock_run

        worker._ffmpeg_fallback_encode = MagicMock(return_value=True)

        logs = []
        worker.log_msg = capture_signal(logs)
        worker._current_temps = []
        worker._stop = False  # will be set by the pattern matcher

        output_f = tmp_path / "output" / "input_archived.mkv"
        result = worker._encode_one(test_video, test_video, output_f, 1)

        # Should fail — systematic issue, no retry
        assert result is False
        assert worker._stop is True, "VSScript failure should set _stop"
        # _ffmpeg_fallback_encode should NOT have been called (self._stop is True)
        worker._ffmpeg_fallback_encode.assert_not_called()
        # Should NOT have logged RETRY
        assert not any("RETRY" in l for l in logs)
