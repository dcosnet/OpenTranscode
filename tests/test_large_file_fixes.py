"""v4.6.0 large-file + av1an chunking fix tests.

Root causes these lock in:
  1. The pure-ffmpeg path pre-scaled to a CRF-16 intermediate — a flat
     1800s timeout and a 0.5-0.8x source size temp file killed large
     files. Now the ffmpeg path scales inline and only the av1an path
     pre-scales (with the per-file timeout and STOP support).
  2. av1an's FRAME MISMATCH / "encoder crashed: exit status: 0" chunk
     drift fell through to "Unknown av1an failure"; the ffmpeg ≥ 7
     "-vsync" removal broke segment/hybrid chunking silently. Both are
     now diagnosed and trigger the select retry.
  3. Severe disk-space warnings were verbose-only, so quiet mode gave
     zero notice before "No space left on device".
  4. Loudnorm analysis decoded the whole VIDEO stream (-af only), pushing
     large files past its 120s timeout; -vn makes it audio-only.
"""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from conftest import capture_signal

from opentranscode.codec_profiles import (
    AUDIO_PROFILES,
    CONTAINER_PROFILES,
    RESOLUTION_PRESETS,
    VIDEO_CODECS,
)
from opentranscode.encoder_worker import EncoderWorker


def _worker(mock_env, use_ffmpeg_fallback, codec=None):
    return EncoderWorker(
        in_dir=Path("/tmp"),
        out_dir=Path("/tmp"),
        video_codec=codec or VIDEO_CODECS[0],
        audio_profile=AUDIO_PROFILES[0],
        container=CONTAINER_PROFILES[0],
        crf=32,
        preset_label="Medium (6)",
        delete_source=False,
        env=mock_env,
        extensions={".mkv"},
        resolution=RESOLUTION_PRESETS[0],
        use_ffmpeg_fallback=use_ffmpeg_fallback,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  _prepare_input: pre-scale only on the av1an path
# ─────────────────────────────────────────────────────────────────────────────

class TestPreScaleGating:
    def test_ffmpeg_path_skips_prescale(self, mock_env, tmp_path):
        """With the ffmpeg path (incl. GPU mode), the source file is used
        directly — no CRF-16 intermediate, no 30-minute pre-scale cap."""
        w = _worker(mock_env, use_ffmpeg_fallback=True)
        w._temp_dir = tmp_path
        w._current_temps = []
        w.resolution = RESOLUTION_PRESETS[2]  # 1080p — scaling requested
        w.verbose = False

        src = tmp_path / "source.mp4"
        src.write_bytes(b"\x00" * 1024)

        mock_env.ffmpeg_path = "/usr/bin/ffmpeg"
        result = w._prepare_input(
            src, 1920, 1080, needs_scale=True,
            scale_filter="scale=1920:1080",
        )
        assert result is not None
        encode_input, output_f = result
        assert encode_input == src  # NO intermediate
        # No pre-scale intermediate tracked (the .av1an work-dir path is
        # still tracked by the symlink section — harmless, never created
        # on this path).
        assert not any(".scaled_tmp" in str(t) for t in w._current_temps)

    def test_av1an_path_still_prescales(self, mock_env, tmp_path, monkeypatch):
        """The av1an path keeps the CRF-16 intermediate (VS plugins need
        it) but now runs it via _run_with_stop_check with the per-file
        timeout instead of a flat 1800s subprocess.run cap."""
        w = _worker(mock_env, use_ffmpeg_fallback=False)
        w._temp_dir = tmp_path
        w._current_temps = []
        w.resolution = RESOLUTION_PRESETS[2]
        w.verbose = False
        w.encode_timeout = 86400
        logs: list[str] = []
        w.log_msg = capture_signal(logs)

        src = tmp_path / "source.mp4"
        src.write_bytes(b"\x00" * 1024)

        seen = {}

        def fake_stop_check(cmd, env=None, timeout=None, log_prefix="  "):
            seen["timeout"] = timeout
            seen["cmd"] = cmd
            # Fake a successful pre-scale: create the intermediate.
            out_idx = cmd.index("-y")
            Path(cmd[out_idx + 1]).write_bytes(b"\x00" * 4096)
            return ("ok", 0, "", "")

        w._run_with_stop_check = fake_stop_check

        result = w._prepare_input(
            src, 1920, 1080, needs_scale=True,
            scale_filter="scale=1920:1080",
        )
        assert result is not None
        encode_input, output_f = result
        assert encode_input != src  # intermediate produced
        assert str(encode_input).endswith(".scaled_tmp.mkv")
        assert seen["timeout"] == 86400  # per-file timeout, NOT 1800
        assert any("libx265" in c for c in seen["cmd"])  # CRF-16 intermediate

    def test_prescale_timeout_fails_cleanly(self, mock_env, tmp_path):
        """A pre-scale timeout now reports a dedicated message (the old
        code surfaced a raw TimeoutExpired as 'pre-scale error')."""
        w = _worker(mock_env, use_ffmpeg_fallback=False)
        w._temp_dir = tmp_path
        w._current_temps = []
        w.resolution = RESOLUTION_PRESETS[2]
        w.verbose = False
        w.encode_timeout = 3600
        w.fail_count = 0
        logs: list[str] = []
        w.log_msg = capture_signal(logs)

        src = tmp_path / "source.mp4"
        src.write_bytes(b"\x00" * 1024)
        w._run_with_stop_check = MagicMock(return_value=("timeout", -1, "", ""))

        result = w._prepare_input(
            src, 1920, 1080, needs_scale=True,
            scale_filter="scale=1920:1080",
        )
        assert result is None
        assert w.fail_count == 1
        assert any("pre-scale timeout" in l for l in logs)

    def test_prescale_stop_aborts_without_fail(self, mock_env, tmp_path):
        """User STOP during pre-scale is not a transcode failure."""
        w = _worker(mock_env, use_ffmpeg_fallback=False)
        w._temp_dir = tmp_path
        w._current_temps = []
        w.resolution = RESOLUTION_PRESETS[2]
        w.verbose = False
        w.fail_count = 0
        logs: list[str] = []
        w.log_msg = capture_signal(logs)

        src = tmp_path / "source.mp4"
        src.write_bytes(b"\x00" * 1024)
        w._run_with_stop_check = MagicMock(return_value=("stop", -1, "", ""))

        result = w._prepare_input(
            src, 1920, 1080, needs_scale=True,
            scale_filter="scale=1920:1080",
        )
        assert result is None
        assert w.fail_count == 0


# ─────────────────────────────────────────────────────────────────────────────
#  Disk-space severe warnings are user-facing
# ─────────────────────────────────────────────────────────────────────────────

class TestDiskSpaceWarnings:
    def _setup(self, mock_env, tmp_path, verbose):
        w = _worker(mock_env, use_ffmpeg_fallback=True)
        w._temp_dir = tmp_path
        w.verbose = verbose
        logs: list[str] = []
        w.log_msg = capture_signal(logs)
        src = tmp_path / "big.mkv"
        src.write_bytes(b"\x00" * (2 * 1024 ** 3))  # 2 GB (sparse-ish; st_size counts)
        return w, logs, src

    def test_severe_output_warning_in_quiet_mode(self, mock_env, tmp_path, monkeypatch):
        w, logs, src = self._setup(mock_env, tmp_path, verbose=False)
        monkeypatch.setattr(
            "opentranscode.encoder_worker.shutil.disk_usage",
            lambda _p: MagicMock(free=512 * 1024 ** 2),  # 0.5 GB free < 2 GB
        )
        w._check_disk_space(src, tmp_path / "out.mkv", needs_scale=False)
        assert any("low disk space on output" in l for l in logs), logs

    def test_no_warning_when_plenty_free_quiet(self, mock_env, tmp_path, monkeypatch):
        w, logs, src = self._setup(mock_env, tmp_path, verbose=False)
        monkeypatch.setattr(
            "opentranscode.encoder_worker.shutil.disk_usage",
            lambda _p: MagicMock(free=100 * 1024 ** 3),
        )
        w._check_disk_space(src, tmp_path / "out.mkv", needs_scale=False)
        assert logs == []

    def test_severe_temp_warning_in_quiet_mode_av1an_scale(
        self, mock_env, tmp_path, monkeypatch,
    ):
        w, logs, src = self._setup(mock_env, tmp_path, verbose=False)
        w.use_ffmpeg_fallback = False  # av1an path — intermediate on temp
        calls = {"n": 0}

        def fake_disk_usage(_p):
            calls["n"] += 1
            # First call: output partition (plenty); second: temp (low).
            free = 100 * 1024 ** 3 if calls["n"] == 1 else 512 * 1024 ** 2
            return MagicMock(free=free)

        monkeypatch.setattr("opentranscode.encoder_worker.shutil.disk_usage",
                            fake_disk_usage)
        w._check_disk_space(src, tmp_path / "out.mkv", needs_scale=True)
        assert any("low disk space on temp" in l for l in logs), logs


# ─────────────────────────────────────────────────────────────────────────────
#  Loudnorm analysis: audio-only decode
# ─────────────────────────────────────────────────────────────────────────────

class TestLoudnormAudioOnly:
    def test_analysis_cmd_has_vn(self, mock_env, tmp_path):
        w = _worker(mock_env, use_ffmpeg_fallback=True)
        w.audio_level_db = -16.0  # enable normalization path
        mock_env.ffmpeg_path = "/usr/bin/ffmpeg"

        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            res = MagicMock(returncode=1)
            res.stdout = ""
            res.stderr = "no json"  # no JSON → returns None (fine)
            return res

        # _analyze_audio_loudness uses subprocess.run directly
        import opentranscode.encoder_worker as ew
        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(ew.subprocess, "run", fake_run)
            gain = w._analyze_audio_loudness(tmp_path / "video.mp4")
        finally:
            monkeypatch.undo()

        assert gain is None  # stderr had no JSON — graceful fallback
        assert "-vn" in seen["cmd"], (
            "loudnorm analysis must skip video decoding (-vn) or large "
            "files blow the 120s timeout"
        )


# ─────────────────────────────────────────────────────────────────────────────
#  av1an chunking: new failure patterns trigger the select retry
# ─────────────────────────────────────────────────────────────────────────────

_FRAME_MISMATCH_STDERR = (
    "ERROR av1an_core::broker: [chunk 2] encoder failed 3 times, shutting "
    "down worker: encoder crashed: exit status: 0\n"
)
_FRAME_MISMATCH_STDOUT = (
    "        FRAME MISMATCH: chunk 2: 53/52 (actual/expected frames)\n"
)
_VSYNC_STDERR = (
    "Error: FFmpeg failed to segment:\n"
    '    stderr: "Unrecognized option \'vsync\'.\\nError splitting the '
    'argument list: Option not found\\n"\n'
)


class TestChunkFailurePatterns:
    def _encode_one_with_mocks(self, mock_env, tmp_path, stdout, stderr):
        w = _worker(mock_env, use_ffmpeg_fallback=False)
        w._temp_dir = tmp_path
        w._current_temps = []
        w._file_res_map = {}
        w._stop = False
        w._consecutive_fail_count = 0
        w._last_fail_pattern = None
        w.verbose = True
        w.audio_level_db = 0.0
        w.encode_timeout = 60
        w._current_idx = 1
        w._current_total = 1
        w._current_filename = "test.mkv"
        w._current_scale_filter = ""
        mock_env.av1an_flags["svt_name"] = "svt-av1"
        # conftest's mock_env pre-sets the env_probe "select" override;
        # remove it so the first attempt runs with auto (the retry path
        # under test).
        mock_env.av1an_flags.pop("chunk_method_override", None)
        logs: list[str] = []
        w.log_msg = capture_signal(logs)

        calls: list[str | None] = []

        def fake_stop_check(cmd, env=None, timeout=None, log_prefix="  "):
            cm = None
            if "--chunk-method" in cmd:
                cm = cmd[cmd.index("--chunk-method") + 1]
            calls.append(cm)
            if cm in (None, "hybrid"):
                return ("ok", 1, stdout, stderr)
            # select retry: fake a valid output
            out = Path(cmd[cmd.index("-o") + 1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"\x00" * 8192)
            return ("ok", 0, "", "encoding finished")

        w._run_with_stop_check = fake_stop_check
        w._ffmpeg_fallback_encode = MagicMock(return_value=True)

        src = tmp_path / "input.mkv"
        src.write_bytes(b"\x00" * 4096)
        out = tmp_path / "out" / "input_archived.mkv"
        result = w._encode_one(src, src, out, 1)
        return w, logs, calls, result

    def test_frame_mismatch_triggers_select_retry(self, mock_env, tmp_path):
        """FRAME MISMATCH (frames land in av1an's STDOUT) previously fell
        to 'Unknown av1an failure'; now it retries with select."""
        w, logs, calls, result = self._encode_one_with_mocks(
            mock_env, tmp_path, _FRAME_MISMATCH_STDOUT, _FRAME_MISMATCH_STDERR,
        )
        assert result is True
        assert calls == [None, "select"], calls
        assert w.env.av1an_flags.get("chunk_method_override") == "select"

    def test_vsync_removal_diagnosed_and_retried(self, mock_env, tmp_path):
        """ffmpeg ≥ 7 removed -vsync; av1an segment chunking dies with
        'Unrecognized option' — must be diagnosed, not 'Unknown'."""
        w, logs, calls, result = self._encode_one_with_mocks(
            mock_env, tmp_path, "", _VSYNC_STDERR,
        )
        assert result is True
        assert calls == [None, "select"]
        assert any("vsync" in l for l in logs), logs

    def test_frame_mismatch_not_misdiagnosed_as_concat(self, mock_env, tmp_path):
        w, logs, calls, result = self._encode_one_with_mocks(
            mock_env, tmp_path, _FRAME_MISMATCH_STDOUT, _FRAME_MISMATCH_STDERR,
        )
        assert not any("concat failure" in l for l in logs), logs
