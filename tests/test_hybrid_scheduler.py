"""v4.7.0 hybrid (GPU + CPU lanes) scheduler tests.

Covers:
  - ``plan_hybrid``: degenerate cases, LPT size-balanced split, CPU
    thread budget, unknown-size fallback
  - ``EncoderWorker.file_subset``: a lane only processes its partition
  - ``ffmpeg_threads``: injected on the CPU software path, never GPU
  - per-lane temp dirs (``_worker_temp_dir`` lane suffix)
  - ``scan_input_files``: extension filter + intermediate exclusion
  - CLI ``--engine hybrid``
  - launcher parity (plan_hybrid + lane params exist)
"""
import inspect
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
from opentranscode.encoder_worker import EncoderWorker, scan_input_files
from opentranscode.hybrid_scheduler import (
    HYBRID_CPU_RESERVE_THREADS,
    HybridPlan,
    plan_hybrid,
)


def _hevc():
    return next(c for c in VIDEO_CODECS if "x265" in c.label)


def _files(tmp_path, names):
    files = []
    for i, name in enumerate(names):
        f = tmp_path / name
        f.write_bytes(b"\x00" * 1024)
        files.append(f)
    return files


# ─────────────────────────────────────────────────────────────────────────────
#  plan_hybrid
# ─────────────────────────────────────────────────────────────────────────────

class TestPlanHybrid:
    def test_no_gpu_encoder_returns_none(self, tmp_path):
        files = _files(tmp_path, ["a.mp4", "b.mp4"])
        assert plan_hybrid(files, "", True, 28) is None
        assert plan_hybrid(files, "hevc_nvenc", False, 28) is None
        assert plan_hybrid(files, "hevc_nvenc", True, 28) is not None

    def test_empty_file_list_returns_none(self):
        assert plan_hybrid([], "hevc_nvenc", True, 28) is None

    def test_single_file_goes_to_gpu_lane(self, tmp_path):
        files = _files(tmp_path, ["a.mp4"])
        plan = plan_hybrid(files, "hevc_nvenc", True, 28)
        assert plan.gpu_files == files
        assert plan.cpu_files == []

    def test_split_covers_all_files_exactly_once(self, tmp_path):
        files = _files(tmp_path, [f"v{i}.mp4" for i in range(10)])
        sizes = {f: (i + 1) * 100_000_000 for i, f in enumerate(files)}
        plan = plan_hybrid(files, "hevc_nvenc", True, 28, sizes=sizes)
        assert plan.total_files == len(files)
        assert sorted(plan.gpu_files + plan.cpu_files) == sorted(files)
        assert not set(plan.gpu_files) & set(plan.cpu_files)

    def test_lpt_prefers_gpu_for_largest_files(self, tmp_path):
        """The largest file goes to the GPU lane (lowest per-byte cost),
        and the greedy split keeps the smaller CPU load in check."""
        files = _files(tmp_path, ["big.mp4", "small.mp4"])
        sizes = {files[0]: 1_000_000_000, files[1]: 10_000_000}
        plan = plan_hybrid(files, "hevc_nvenc", True, 28, sizes=sizes)
        assert plan.gpu_files[0] == files[0]  # biggest → GPU

    def test_cpu_budget_reserves_gpu_threads(self, tmp_path):
        files = _files(tmp_path, ["a.mp4", "b.mp4"])
        plan = plan_hybrid(files, "hevc_nvenc", True, 28)
        assert plan.cpu_budget_threads == 28 - HYBRID_CPU_RESERVE_THREADS
        plan2 = plan_hybrid(files, "hevc_nvenc", True, 2)
        assert plan2.cpu_budget_threads == 1  # never below 1

    def test_unknown_sizes_fall_back_to_average(self, tmp_path):
        a, b, c = _files(tmp_path, ["a.mp4", "b.mp4", "c.mp4"])
        sizes = {a: 1000, b: 1000}  # c unknown
        plan = plan_hybrid([a, b, c], "hevc_nvenc", True, 28, sizes=sizes)
        assert plan.total_files == 3  # no crash, all files placed

    def test_plan_is_pure_dataclass(self):
        plan = HybridPlan()
        assert plan.total_files == 0
        assert plan.gpu_encoder == ""
        assert plan.cpu_budget_threads == 1


# ─────────────────────────────────────────────────────────────────────────────
#  EncoderWorker hybrid support
# ─────────────────────────────────────────────────────────────────────────────

def _worker(mock_env, tmp_path, **kw):
    return EncoderWorker(
        in_dir=tmp_path / "in",
        out_dir=tmp_path / "out",
        video_codec=_hevc(),
        audio_profile=AUDIO_PROFILES[2],
        container=CONTAINER_PROFILES[0],
        crf=28,
        preset_label="Faster (10)",
        delete_source=False,
        env=mock_env,
        extensions={".mkv", ".mp4"},
        resolution=RESOLUTION_PRESETS[0],
        **kw,
    )


class TestWorkerHybridSupport:
    def test_file_subset_restricts_queue(self, mock_env, tmp_path, tiny_test_video):
        # Real videos — the worker ffprobe-validates its queue.
        (tmp_path / "in").mkdir(parents=True)
        a = tmp_path / "in" / "a.mkv"
        b = tmp_path / "in" / "b.mkv"
        a.write_bytes(tiny_test_video.read_bytes())
        b.write_bytes(tiny_test_video.read_bytes())
        (tmp_path / "out").mkdir(parents=True)

        mock_env.ffmpeg_libs["hevc_nvenc"] = True
        mock_env.gpu.functional["hevc_nvenc"] = True  # live probe passes
        w = _worker(mock_env, tmp_path, file_subset=[a], lane="gpu",
                    engine="gpu", use_ffmpeg_fallback=True)
        logs: list[str] = []
        w.log_msg = capture_signal(logs)
        w._run_with_stop_check = MagicMock(return_value=("ok", 0, "", ""))
        # Fake a successful output for the encoded file.
        def fake_run(cmd, **kw2):
            # ffmpeg path: output is the last arg (after -y)
            out = Path(cmd[-1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"\x00" * 8192)
            return ("ok", 0, "", "")
        w._run_with_stop_check = fake_run
        w.audio_level_db = 0.0
        w.verbose = False
        w.run()

        assert w.success_count == 1
        # Only the subset file was encoded.
        assert (tmp_path / "out" / "a_archived.mkv").exists()
        assert not (tmp_path / "out" / "b_archived.mkv").exists()

    def test_lane_temp_dirs_are_distinct(self, mock_env, tmp_path):
        gpu = _worker(mock_env, tmp_path, lane="gpu")
        cpu = _worker(mock_env, tmp_path, lane="cpu")
        plain = _worker(mock_env, tmp_path)
        assert gpu._temp_dir != cpu._temp_dir
        assert "-gpu" in gpu._temp_dir.name
        assert "-cpu" in cpu._temp_dir.name
        assert "-" not in plain._temp_dir.name.replace("worker-", "").lstrip("0123456789") or \
               plain._temp_dir.name == f"worker-{plain._temp_dir.name.split('-')[1]}"

    def test_ffmpeg_threads_on_cpu_software_path(self, mock_env, tmp_path):
        w = _worker(mock_env, tmp_path, ffmpeg_threads=13,
                    use_ffmpeg_fallback=True, engine="cpu")
        w.resolution = RESOLUTION_PRESETS[0]
        w.audio_level_db = 0.0
        w.encode_timeout = 60
        w._gpu_encoder = None  # CPU lane
        w._run_with_stop_check = MagicMock(return_value=("ok", 0, "", ""))
        src = tmp_path / "in" / "x.mp4"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"\x00" * 64)
        w._ffmpeg_fallback_encode(src, src, tmp_path / "out.mkv")
        cmd = w._run_with_stop_check.call_args[0][0]
        assert "-threads" in cmd and "13" in cmd

    def test_failed_ffmpeg_encode_removes_partial_output(self, mock_env, tmp_path):
        """A failed ffmpeg encode must not leave a truncated file that
        skip-existing could later mistake for a finished archive."""
        w = _worker(mock_env, tmp_path, use_ffmpeg_fallback=True, engine="cpu")
        w.resolution = RESOLUTION_PRESETS[0]
        w.audio_level_db = 0.0
        w.encode_timeout = 60
        w._gpu_encoder = None
        w._run_with_stop_check = MagicMock(return_value=("ok", 1, "", "boom"))
        src = tmp_path / "in" / "x.mp4"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"\x00" * 64)
        out = tmp_path / "out.mkv"
        out.write_bytes(b"partial")  # ffmpeg left a partial file
        ok = w._ffmpeg_fallback_encode(src, src, out)
        assert ok is False
        assert not out.exists(), "partial output must be removed on failure"

    def test_no_threads_cap_on_gpu_path(self, mock_env, tmp_path):
        mock_env.ffmpeg_libs["hevc_nvenc"] = True
        w = _worker(mock_env, tmp_path, ffmpeg_threads=13,
                    use_ffmpeg_fallback=True, engine="gpu")
        w.resolution = RESOLUTION_PRESETS[0]
        w.audio_level_db = 0.0
        w.encode_timeout = 60
        w._gpu_encoder = "hevc_nvenc"  # GPU lane — NVENC is silicon-bound
        w._run_with_stop_check = MagicMock(return_value=("ok", 0, "", ""))
        src = tmp_path / "in" / "x.mp4"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"\x00" * 64)
        w._ffmpeg_fallback_encode(src, src, tmp_path / "out.mkv")
        cmd = w._run_with_stop_check.call_args[0][0]
        assert "-threads" not in cmd
        assert "hevc_nvenc" in cmd


# ─────────────────────────────────────────────────────────────────────────────
#  scan_input_files
# ─────────────────────────────────────────────────────────────────────────────

class TestScanInputFiles:
    def test_filters_extensions_and_intermediates(self, tmp_path):
        keep = tmp_path / "keep.mp4"; keep.write_bytes(b"\x00")
        drop = tmp_path / "drop.txt"; drop.write_bytes(b"\x00")
        inter = tmp_path / "keep.scaled_tmp.mkv"; inter.write_bytes(b"\x00")
        nested = tmp_path / "sub"; nested.mkdir()
        n2 = nested / "nested.mkv"; n2.write_bytes(b"\x00")
        result = scan_input_files(tmp_path, {".mp4", ".mkv"})
        assert keep in result and n2 in result
        assert drop not in result
        assert inter not in result

    def test_sorted_output(self, tmp_path):
        for name in ("c.mp4", "a.mp4", "b.mp4"):
            (tmp_path / name).write_bytes(b"\x00")
        assert scan_input_files(tmp_path, {".mp4"}) == sorted(
            scan_input_files(tmp_path, {".mp4"}))


# ─────────────────────────────────────────────────────────────────────────────
#  CLI + launcher parity
# ─────────────────────────────────────────────────────────────────────────────

class TestCliHybrid:
    def test_engine_hybrid_accepted(self):
        from opentranscode.cli import build_parser
        assert build_parser().parse_args(["--engine", "hybrid"]).engine == "hybrid"

    def test_engine_default_still_auto(self):
        from opentranscode.cli import build_parser
        assert build_parser().parse_args([]).engine == "auto"


class TestLauncherHybridParity:
    def test_launcher_has_plan_hybrid(self, opentranscode_module):
        assert hasattr(opentranscode_module, "plan_hybrid")
        assert hasattr(opentranscode_module, "HybridPlan")

    def test_launcher_worker_lane_params(self, opentranscode_module):
        sig = inspect.signature(opentranscode_module.EncoderWorker.__init__)
        for p in ("file_subset", "lane", "ffmpeg_threads"):
            assert p in sig.parameters, p

    def test_launcher_plan_matches_package_behavior(self, opentranscode_module, tmp_path):
        files = _files(tmp_path, ["a.mp4", "b.mp4", "c.mp4", "d.mp4"])
        plan = opentranscode_module.plan_hybrid(
            files, "hevc_nvenc", True, 28,
            sizes={f: i * 10_000_000 for i, f in enumerate(files)},
        )
        pkg_plan = plan_hybrid(
            files, "hevc_nvenc", True, 28,
            sizes={f: i * 10_000_000 for i, f in enumerate(files)},
        )
        assert [Path(x) for x in plan.gpu_files] == pkg_plan.gpu_files
        assert [Path(x) for x in plan.cpu_files] == pkg_plan.cpu_files
