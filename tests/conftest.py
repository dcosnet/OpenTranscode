"""
Shared pytest fixtures and PySide6 stubs for the OpenTranscode test suite.

Why this file exists
--------------------
The ``open-transcode.py`` launcher script is a PySide6 GUI that imports
``PySide6.QtWidgets`` / ``QtCore`` / ``QtGui`` at module load time. The
non-UI unit tests in this suite (smoke test, encoder pipeline, audio
loudnorm, subtitle mux, stop-button, concurrent workers) only need the
*non-Qt* logic (dataclasses, free functions, and the non-Qt methods of
``EncoderWorker``). They should run on any CI worker — even one without a
real PySide6 install.

To make that possible, this conftest installs *stub* PySide6 modules in
``sys.modules`` BEFORE the open-transcode module is loaded, but only when a real PySide6
package is not available. The stubs provide:

  - Real Python base classes for ``QThread``, ``QWidget``, ``QMainWindow``
    so that ``class EncoderWorker(QThread)`` and ``class OpenCodecMaster(
    QMainWindow)`` succeed at module load time. The stub ``__init__`` methods
    accept any args/kwargs so ``super().__init__()`` calls in the real
    ``__init__`` methods don't raise.
  - ``MagicMock`` for everything else (``QApplication``, ``QVBoxLayout``,
    ``QFont``, ``Qt`` enum, ``Signal``, ``Slot``, etc.) so attribute access
    and instantiation are no-ops.

If a real PySide6 IS installed, the stubs are NOT installed and the open-transcode module
loads against the real Qt classes. All tests in this suite work in both
modes — they either instantiate ``EncoderWorker`` via ``__new__`` (bypassing
``QThread.__init__``) or via ``__init__`` (which is safe to call because it
does not start the QThread).
"""

from __future__ import annotations

import importlib.util
import io
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ─────────────────────────────────────────────────────────────────────────────
#  PySide6 detection + stub installation
# ─────────────────────────────────────────────────────────────────────────────

# Detect a REAL PySide6 install BEFORE installing any stubs. We use
# importlib.util.find_spec (not "import PySide6") so that we don't trigger
# PySide6's somewhat expensive C-extension load if it IS installed.
_REAL_PYSIDE6_AVAILABLE: bool = importlib.util.find_spec("PySide6") is not None


def _install_pyside6_stubs() -> None:
    """Install stub PySide6 modules in ``sys.modules``.

    Idempotent: a second call is a no-op (detected via the ``_otc_stub``
    marker on the fake ``PySide6`` package).
    """
    if getattr(sys.modules.get("PySide6"), "_otc_stub", False):
        return  # already installed

    class _StubBase:
        """Minimal base for stubbed Qt objects.

        Accepts any args/kwargs in ``__init__`` so subclass ``__init__``
        methods that call ``super().__init__(...)`` don't fail. Auto-returns
        a ``MagicMock`` for any attribute not explicitly defined, so methods
        like ``setObjectName``, ``resize``, ``setLayout`` are no-ops.
        """

        def __init__(self, *args, **kwargs):
            pass

        def __getattr__(self, name):
            m = MagicMock()
            # Bypass __setattr__ (which would otherwise hit __getattr__ again
            # for non-existent dunder lookups during interpreter bootstrapping).
            object.__setattr__(self, name, m)
            return m

    class _StubQWidget(_StubBase):
        pass

    class _StubQMainWindow(_StubQWidget):
        pass

    class _StubQThread(_StubBase):
        # QThread class-level signals (defined as MagicMock instances so
        # ``worker.started.connect(...)`` works without raising).
        started = MagicMock()
        finished = MagicMock()

        def start(self, *args, **kwargs):
            pass

        def wait(self, *args, **kwargs):
            return True

        def terminate(self):
            pass

        def isRunning(self):
            return False

        def requestInterruption(self):
            pass

        def isInterruptionRequested(self):
            return False

    # Build the fake PySide6.QtCore module.
    qtcore = MagicMock()
    qtcore.QThread = _StubQThread
    qtcore.Qt = MagicMock()
    # Signal(str) must return something with .emit(). Use a side_effect so
    # each call returns a fresh MagicMock (matching the real Signal behavior
    # of returning a per-class-attribute signal instance).
    qtcore.Signal = MagicMock(side_effect=lambda *a, **k: MagicMock())
    # Slot is used as a decorator: @Slot() -> (fn -> fn).
    qtcore.Slot = lambda *a, **k: (lambda f: f)
    qtcore.QTimer = MagicMock()
    qtcore.QPointF = MagicMock()
    qtcore.QRectF = MagicMock()

    # Build the fake PySide6.QtWidgets module.
    qtwidgets = MagicMock()
    qtwidgets.QWidget = _StubQWidget
    qtwidgets.QMainWindow = _StubQMainWindow
    # Other names (QApplication, QVBoxLayout, QLabel, ...) auto-resolve to
    # child MagicMocks via the parent MagicMock's attribute access.

    # Build the fake PySide6.QtGui module.
    qtgui = MagicMock()

    # Assemble the fake PySide6 package.
    pyside6 = MagicMock()
    pyside6._otc_stub = True  # idempotency marker
    pyside6.QtCore = qtcore
    pyside6.QtWidgets = qtwidgets
    pyside6.QtGui = qtgui

    sys.modules["PySide6"] = pyside6
    sys.modules["PySide6.QtCore"] = qtcore
    sys.modules["PySide6.QtWidgets"] = qtwidgets
    sys.modules["PySide6.QtGui"] = qtgui


if not _REAL_PYSIDE6_AVAILABLE:
    _install_pyside6_stubs()


# ─────────────────────────────────────────────────────────────────────────────
#  open-transcode.py module loader
# ─────────────────────────────────────────────────────────────────────────────

OPENTRANSCODE_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "open-transcode.py"


@pytest.fixture(scope="session")
def opentranscode_module():
    """Load ``open-transcode.py`` as a Python module.

    The filename contains a dash (illegal in Python identifiers), so we
    use ``importlib.util.spec_from_file_location``. The module is loaded
    once per test session (scope="session") and cached here.
    """
    if not OPENTRANSCODE_SCRIPT_PATH.is_file():
        pytest.skip(f"open-transcode.py not found at {OPENTRANSCODE_SCRIPT_PATH}")
    spec = importlib.util.spec_from_file_location(
        "open_transcode", str(OPENTRANSCODE_SCRIPT_PATH)
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ─────────────────────────────────────────────────────────────────────────────
#  Shared fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def tiny_test_video(tmp_path):
    """Create a 1-second 64x64 black video using ffmpeg.

    If ffmpeg is not installed, returns a fake path. Tests that need a real
    video file should skip themselves when this fixture returns a path that
    does not exist on disk; most tests in this suite instead mock
    ``subprocess.run`` and never touch a real video.
    """
    ffmpeg_bin = shutil.which("ffmpeg")
    if ffmpeg_bin is None:
        # ffmpeg not installed — return a fake path. Callers that need a
        # real file should check ``.exists()`` and skip / mock accordingly.
        return tmp_path / "fake_test_video.mkv"

    out = tmp_path / "tiny_test_video.mkv"
    try:
        subprocess.run(
            [
                ffmpeg_bin,
                "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1:r=24",
                "-t", "1", "-pix_fmt", "yuv420p", "-an", "-y", str(out),
            ],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return tmp_path / "fake_test_video.mkv"
    if not out.exists():
        return tmp_path / "fake_test_video.mkv"
    return out


@pytest.fixture
def mock_env(opentranscode_module):
    """Return a fully-populated fake ``EnvProbe`` for testing.

    Every field is set to a plausible value so tests that read ``env.X``
    don't have to construct the whole distro/cpu topology themselves.
    """
    EnvProbe = opentranscode_module.EnvProbe
    DistroProfile = opentranscode_module.DistroProfile
    CpuTopology = opentranscode_module.CpuTopology

    distro = DistroProfile(
        family="debian",
        name="Ubuntu 24.04",
        version_id="24.04",
        pkg_manager="apt",
        install_cmd_template="sudo apt install {packages}",
        binary_extra_paths=["/usr/bin", "/usr/local/bin"],
        av1an_known_encoder_names=["svt_av1", "svt-av1"],
        ffmpeg_pkg="ffmpeg",
        av1an_pkg="av1an",
        notes="test distro profile",
    )
    cpu = CpuTopology(
        physical_cores=4,
        logical_threads=8,
        threads_per_core=2,
        model_name="Test CPU @ 2.0 GHz",
    )

    env = EnvProbe()
    env.distro = distro
    env.av1an_path = "/usr/bin/av1an"
    env.ffmpeg_path = "/usr/bin/ffmpeg"
    env.ffprobe_path = "/usr/bin/ffprobe"
    env.av1an_flags = {
        "worker": "--workers",
        "video_params": "--video-params",
        "audio_params": "--audio-params",
        "concat_method": "ffmpeg",
        "chunk_method_override": "select",
        "svt_name": "svt_av1",
        "has_chunk_method": True,
    }
    env.av1an_version = "0.5.2"
    env.ffmpeg_version = "6.0"
    env.ffmpeg_libs = {
        "libsvtav1": True,
        "libaom": True,
        "libvpx": True,
        "libx265": True,
        "libopus": True,
        "libvorbis": True,
        "flac": True,
    }
    env.runtime_deps = {}
    env.missing_dep_pkgs = []
    env.vs_version = "R65"
    env.vs_script_lib = "/usr/lib/x86_64-linux-gnu/libvapoursynth-script.so"
    env.cpu = cpu
    env.errors = []
    env.warnings = []
    return env


@pytest.fixture
def mock_subprocess_run(monkeypatch):
    """Patch ``subprocess.run`` to return configurable ``CompletedProcess`` objects.

    Returns a mutable ``list`` that tests populate with the results they want
    returned (or exceptions to raise) in call order. Each entry is either a
    ``subprocess.CompletedProcess`` (returned as-is), a ``BaseException``
    (raised), or any other object (wrapped in a CompletedProcess with
    returncode=0). Once the list is exhausted, subsequent calls return a
    default rc=0 CompletedProcess.
    """
    results: list = []

    def fake_run(cmd, *args, **kwargs):
        if results:
            r = results.pop(0)
            if isinstance(r, BaseException):
                raise r
            if isinstance(r, subprocess.CompletedProcess):
                return r
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=str(r), stderr="",
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    return results


@pytest.fixture
def mock_subprocess_popen(monkeypatch):
    """Patch ``subprocess.Popen`` for STOP-button tests.

    Returns a ``MagicMock`` representing the fake subprocess. Tests configure
    it (e.g. ``fake.poll.side_effect = [None, None, 0]``,
    ``fake.wait.side_effect = [...]``) before triggering the code under test.

    ``stdout`` and ``stderr`` default to empty ``StringIO`` objects so the
    open-transcode module's drainer threads (in ``_run_with_stop_check``) immediately
    hit EOF instead of looping forever.
    """
    fake_proc = MagicMock()
    fake_proc.pid = 12345
    fake_proc.stdout = io.StringIO("")
    fake_proc.stderr = io.StringIO("")
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: fake_proc)
    return fake_proc


# ─────────────────────────────────────────────────────────────────────────────
#  Helper functions (importable from any test module via `from conftest import ...`)
# ─────────────────────────────────────────────────────────────────────────────

def make_minimal_worker(opentranscode_module, env=None, audio_level_db=-14.0):
    """Create an ``EncoderWorker`` without running ``__init__``.

    Uses ``EncoderWorker.__new__`` to bypass QThread construction (which
    would require a real Qt event loop in some setups), then sets only the
    attributes the unit tests need. This is the recommended pattern for
    testing the non-Qt methods of ``EncoderWorker`` (``_validate_file``,
    ``_analyze_audio_loudness``, ``_find_subtitle_stream``,
    ``_run_with_stop_check``) in isolation.
    """
    worker = opentranscode_module.EncoderWorker.__new__(opentranscode_module.EncoderWorker)
    worker._stop = False
    worker.log_msg = MagicMock()
    worker._file_res_map = {}
    worker.fail_count = 0
    worker._current_temps = []
    worker._sources_to_delete = []
    worker.success_count = 0
    worker.audio_level_db = audio_level_db
    worker.env = env
    worker.subtitle_lang = None
    worker.use_ffmpeg_fallback = False
    return worker
