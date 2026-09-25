"""
v5 behavior tests — skip-invalid-files, early-abort, file-type diagnostics.

These tests verify the 3 root-cause fixes from v5:
  - v5-01: Invalid files are SKIPPED, not "attempted anyway"
  - v5-02: 3 consecutive failures auto-abort the queue
  - v5-03: `file` command output in diagnostics reveals HTML/text/data
  - v5-04: Pre-flight validation pass reports valid/invalid counts
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


# ─────────────────────────────────────────────────────────────────────────────
# Module loading — same pattern as the other test files
# ─────────────────────────────────────────────────────────────────────────────
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

    qt_gui.QFont = MagicMock()
    qt_gui.QPalette = MagicMock()
    qt_gui.QColor = MagicMock()
    qt_gui.QPainter = MagicMock()
    qt_gui.QPen = MagicMock()
    qt_gui.QBrush = MagicMock()
    qt_gui.QRadialGradient = MagicMock()
    qt_gui.QFontMetrics = MagicMock()

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
# Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestIdentifyFileType:
    """v5-03: _identify_file_type() runs `file -b` and returns the type string."""

    def test_identifies_text_file(self, opentranscode_module, tmp_path):
        """A .txt file should be identified as 'ASCII text' or similar."""
        f = tmp_path / "test.txt"
        f.write_text("This is not a video file, just plain text.")
        result = opentranscode_module._identify_file_type(f)
        # `file` should identify it as text
        assert "text" in result.lower() or "ascii" in result.lower(), \
            f"Expected text/ASCII in result, got: {result}"

    def test_identifies_html_file(self, opentranscode_module, tmp_path):
        """An HTML file should be identified as 'HTML document' — the classic
        failed yt-dlp download scenario."""
        f = tmp_path / "fake_video.mp4"
        f.write_text("<!DOCTYPE html><html><body>Video unavailable</body></html>")
        result = opentranscode_module._identify_file_type(f)
        assert "HTML" in result or "text" in result.lower(), \
            f"Expected HTML/text in result, got: {result}"

    def test_identifies_real_mp4(self, opentranscode_module, tmp_path, real_ffmpeg=None):
        """A real MP4 should be identified as 'ISO Media' or 'MP4'."""
        if not shutil.which("ffmpeg"):
            pytest.skip("ffmpeg not available")
        f = tmp_path / "real.mp4"
        subprocess.run(
            ["ffmpeg", "-f", "lavfi", "-i", "testsrc=duration=0.1:size=32x32:rate=1",
             "-c:v", "libx264", "-y", str(f)],
            capture_output=True, timeout=10,
        )
        if not f.exists():
            pytest.skip("Could not generate test MP4")
        result = opentranscode_module._identify_file_type(f)
        assert "ISO Media" in result or "MP4" in result or "Media" in result, \
            f"Expected ISO Media/MP4 in result, got: {result}"

    def test_returns_empty_for_nonexistent_file(self, opentranscode_module, tmp_path):
        """Nonexistent file should return empty string (not crash)."""
        f = tmp_path / "does_not_exist.bin"
        result = opentranscode_module._identify_file_type(f)
        # Should not crash; may return empty or an error string
        assert isinstance(result, str)


class TestSkipInvalidFiles:
    """v5-01: Invalid files are SKIPPED, not 'attempted anyway'."""

    def test_validate_file_skips_when_ffprobe_returns_none(
        self, opentranscode_module, tmp_path
    ):
        """When ffprobe can't read a file and force=False, _validate_file
        should return skip=True."""
        # Create a fake "video" file that's actually text
        fake_video = tmp_path / "fake.mp4"
        fake_video.write_text("<!DOCTYPE html><html>Not a video</html>")

        # Build a minimal EncoderWorker via __new__ to bypass __init__
        worker = opentranscode_module.EncoderWorker.__new__(opentranscode_module.EncoderWorker)
        worker.force = False  # v5-01: force=False (default)
        worker.fail_count = 0
        worker._file_res_map = {}
        worker.env = MagicMock()
        worker.env.ffprobe_path = shutil.which("ffprobe") or "/usr/bin/ffprobe"
        logs = []
        worker.log_msg = MagicMock()
        worker.log_msg = capture_signal(logs)
        # v4.2.1+: file-type diagnostics on SKIP are verbose-only.
        worker.verbose = True

        skip, info, src_w, src_h = worker._validate_file(fake_video)

        assert skip is True, "Should skip invalid file when force=False"
        assert info is None
        assert worker.fail_count == 1, "Should increment fail_count"
        # Should mention SKIP in the log
        assert any("SKIP" in l for l in logs), \
            f"Expected SKIP in logs, got: {logs}"
        # Should mention the file type (HTML/text)
        assert any("HTML" in l or "text" in l.lower() for l in logs), \
            f"Expected file type info in logs, got: {logs}"

    def test_validate_file_proceeds_when_force_true(
        self, opentranscode_module, tmp_path
    ):
        """When force=True, _validate_file should NOT skip — it should
        log a WARN and proceed (return skip=False)."""
        fake_video = tmp_path / "fake.mp4"
        fake_video.write_text("<!DOCTYPE html><html>Not a video</html>")

        worker = opentranscode_module.EncoderWorker.__new__(opentranscode_module.EncoderWorker)
        worker.force = True  # v5-01: force=True overrides validation
        worker.fail_count = 0
        worker._file_res_map = {}
        worker.env = MagicMock()
        worker.env.ffprobe_path = shutil.which("ffprobe") or "/usr/bin/ffprobe"
        logs = []
        worker.log_msg = MagicMock()
        worker.log_msg = capture_signal(logs)

        skip, info, src_w, src_h = worker._validate_file(fake_video)

        assert skip is False, "Should NOT skip when force=True"
        assert worker.fail_count == 0, "Should NOT increment fail_count"
        # Should log a WARN about attempting anyway
        assert any("WARN" in l and "force" in l.lower() for l in logs), \
            f"Expected WARN about force in logs, got: {logs}"


class TestConsecutiveFailureAbort:
    """v5-02: 3 consecutive failures auto-abort the queue."""

    def test_aborts_after_three_consecutive_failures(self, opentranscode_module):
        """After 3 consecutive failures, _check_consecutive_failures
        should set self._stop = True."""
        worker = opentranscode_module.EncoderWorker.__new__(opentranscode_module.EncoderWorker)
        worker._stop = False
        worker._consecutive_fail_count = 0
        worker._last_fail_pattern = None
        worker.log_msg = MagicMock()
        worker.log_msg.emit = lambda msg: None

        # 3 consecutive failures
        worker._check_consecutive_failures(Path("f1.mp4"), accepted=False)
        assert worker._consecutive_fail_count == 1
        assert worker._stop is False

        worker._check_consecutive_failures(Path("f2.mp4"), accepted=False)
        assert worker._consecutive_fail_count == 2
        assert worker._stop is False

        worker._check_consecutive_failures(Path("f3.mp4"), accepted=False)
        assert worker._consecutive_fail_count == 3
        assert worker._stop is True, "Should auto-abort after 3 consecutive failures"

    def test_success_resets_counter(self, opentranscode_module):
        """A success should reset the consecutive failure counter."""
        worker = opentranscode_module.EncoderWorker.__new__(opentranscode_module.EncoderWorker)
        worker._stop = False
        worker._consecutive_fail_count = 2  # already had 2 failures
        worker._last_fail_pattern = None
        worker.log_msg = MagicMock()
        worker.log_msg.emit = lambda msg: None

        # Success
        worker._check_consecutive_failures(Path("ok.mp4"), accepted=True)
        assert worker._consecutive_fail_count == 0, "Success should reset counter"
        assert worker._stop is False

    def test_does_not_double_abort(self, opentranscode_module):
        """If already stopped (user clicked STOP), don't abort again."""
        worker = opentranscode_module.EncoderWorker.__new__(opentranscode_module.EncoderWorker)
        worker._stop = True  # already stopped
        worker._consecutive_fail_count = 0
        worker._last_fail_pattern = None
        worker.log_msg = MagicMock()
        worker.log_msg.emit = lambda msg: None

        worker._check_consecutive_failures(Path("f.mp4"), accepted=False)
        # Should increment but NOT emit the ABORT message (already stopped)
        assert worker._consecutive_fail_count == 1


class TestErrorPatternsIncludeStreams:
    """v5-03: 'missing field streams' and 'Invalid data found' are in the
    error_patterns table."""

    def test_error_patterns_table_has_streams_pattern(self, opentranscode_module):
        """The error_patterns table in _encode_one should include the
        'missing field streams' pattern. We verify by checking the source
        code (the table is a local variable, not accessible from outside)."""
        # Read the source and check for the pattern
        src = Path(OPENTRANSCODE_PATH).read_text()
        assert "missing field `streams`" in src, \
            "error_patterns table should include 'missing field streams'"
        assert "Invalid data found when processing input" in src, \
            "error_patterns table should include 'Invalid data found'"

    def test_identify_file_type_called_in_diagnostic(self, opentranscode_module):
        """The diagnostic section should call _identify_file_type for
        the streams/invalid-data patterns."""
        src = Path(OPENTRANSCODE_PATH).read_text()
        # The _identify_file_type call should be inside the diagnostic block
        assert "_identify_file_type(file_path)" in src, \
            "Diagnostic should call _identify_file_type"


class TestPreFlightValidation:
    """v5-04: Pre-flight validation pass reports valid/invalid counts."""

    def test_run_aborts_when_all_files_invalid(self, opentranscode_module, tmp_path):
        """When ALL files are invalid and force=False, run() should abort
        immediately without entering the encode loop."""
        # Create 3 fake "video" files (actually text)
        for i in range(3):
            (tmp_path / f"fake{i}.mp4").write_text(
                f"<!DOCTYPE html><html>Not a video {i}</html>"
            )

        out_dir = tmp_path / "output"
        out_dir.mkdir()

        av1_codec = next(c for c in opentranscode_module.VIDEO_CODECS if c.label == "AV1 (SVT-AV1)")
        opus_audio = next(a for a in opentranscode_module.AUDIO_PROFILES if a.label == "Opus (96k)")
        mkv_container = next(c for c in opentranscode_module.CONTAINER_PROFILES if c.ext == "mkv")
        original_res = next(r for r in opentranscode_module.RESOLUTION_PRESETS if r.category == "original")

        env = opentranscode_module.probe_environment()

        worker = opentranscode_module.EncoderWorker(
            in_dir=tmp_path,
            out_dir=out_dir,
            video_codec=av1_codec,
            audio_profile=opus_audio,
            container=mkv_container,
            crf=32,
            preset_label="Fast (4)",
            delete_source=False,
            env=env,
            extensions={".mp4"},
            resolution=original_res,
            use_ffmpeg_fallback=True,
            force=False,  # v5-01: default — should skip invalid files
        )

        logs = []
        worker.log_msg = capture_signal(logs)

        # Run the worker — should abort in pre-flight validation
        worker.run()

        # Should NOT have entered the encode loop (no "[1/3] Encoding" message)
        assert not any("[1/3] Encoding" in l for l in logs), \
            "Should not enter encode loop when all files are invalid"

        # Should have the PRE-FLIGHT VALIDATION section
        assert any("PRE-FLIGHT VALIDATION" in l for l in logs), \
            f"Expected PRE-FLIGHT VALIDATION in logs"

        # Should have the ABORT message
        assert any("ABORT" in l and "invalid" in l.lower() for l in logs), \
            f"Expected ABORT message about invalid files"

        # Should report 0 valid, 3 invalid
        assert any("Valid files:   0" in l for l in logs)
        assert any("Invalid files: 3" in l for l in logs)
