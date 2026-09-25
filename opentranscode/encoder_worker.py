"""EncoderWorker (QThread) — the per-file encode pipeline.

v3-05 split the former 515-line ``run()`` into five single-
responsibility methods (``run`` / ``_process_one_file`` /
``_validate_file`` / ``_prepare_input`` / ``_encode_one`` /
``_verify_and_finalize``). v3-07 added the STOP-button interrupt
(Popen + start_new_session + SIGTERM/SIGKILL on the process group).

Depends on:
  - ``codec_profiles``  — VideoCodecProfile, AudioProfile,
    ContainerProfile, ResolutionProfile, FFMPEG_LIB_KEY_MAP,
    ffmpeg_lib_key_for.
  - ``env_probe``       — EnvProbe (type), _av1an_env.
  - ``ffprobe_utils``   — ffprobe_validate, ffprobe_duration,
    _verify_output_resolution, _identify_file_type.
  - ``temp_manager``    — _temp_path_for, _worker_temp_dir.

"""

import io
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .codec_profiles import (
    FFMPEG_LIB_KEY_MAP,
    AudioProfile,
    ContainerProfile,
    ResolutionProfile,
    VideoCodecProfile,
    ffmpeg_lib_key_for,
)
from .gpu_profiles import (
    encoder_filter_chain,
    encoder_pre_args,
    encoder_quality_args,
    gpu_profile_by_key,
)
from .env_probe import EnvProbe, _av1an_env
from .ffprobe_utils import (
    _identify_file_type,
    _verify_output_resolution,
    ffprobe_duration,
    ffprobe_validate,
)
from .keepawake import KeepAwake
from .temp_manager import _temp_path_for, _worker_temp_dir

# ──────────────────────────────────────────────
#  ENCODER WORKER (QThread, from PySide6 ver, extended)
# ──────────────────────────────────────────────

def scan_input_files(in_dir: Path, extensions: set[str]) -> list[Path]:
    """Collect the transcodable files under *in_dir* (v4.7.0).

    Used by ``EncoderWorker.run()`` for the default whole-directory scan
    AND by the hybrid scheduler's pre-scan, which must partition the
    queue BEFORE the per-lane workers are constructed. Excludes leftover
    pre-scale intermediates from previous failed runs.
    """
    return sorted(
        f for f in in_dir.rglob("*")
        if f.is_file()
        and f.suffix.lower() in extensions
        and not f.name.endswith(".scaled_tmp.mkv")
    )


def selected_gpu_profile(env):
    """v4.8.0: the active GpuProfile — the UI's dropdown selection
    (env.av1an_flags["gpu_profile"]) when set, else the auto-matched
    profile from the probe (env.gpu.profile_key). None when neither."""
    flags = getattr(env, "av1an_flags", None) or {}
    key = flags.get("gpu_profile")
    if key and key != "auto":
        profile = gpu_profile_by_key(key)
        if profile is not None:
            return profile
    gpu_info = getattr(env, "gpu", None)
    if gpu_info is not None and getattr(gpu_info, "profile_key", ""):
        return gpu_profile_by_key(gpu_info.profile_key)
    return None


def resolve_gpu_encoder(engine: str, video_codec, env):
    """Decide whether this encode runs on the GPU.

    Returns ``(encoder_name, api)`` when the GPU path should be used —
    e.g. ``("hevc_nvenc", "nvenc")`` or ``("hevc_vaapi", "vaapi")`` — or
    ``(None, None)`` for the CPU path.

    Rules:
      - ``engine == "cpu"``              → always CPU (user forced CPU).
      - selected/matched GPU profile has
        no encoder for the codec family  → CPU (e.g. AV1 on Pascal,
                                          VP9 without VAAPI).
      - ``engine`` auto/gpu AND the
        functional probe passed for
        that encoder                     → the encoder.

    The gate is ``env.gpu.functional`` — a live encode test run by the
    env probe — NOT the compiled-in ``ffmpeg -encoders`` list, because a
    ffmpeg build can advertise a hardware encoder the installed driver
    is too old to open. Pure function; no I/O. Safe to call from the UI
    thread for a pre-flight status line.
    """
    if engine not in ("auto", "gpu"):
        return (None, None)
    profile = selected_gpu_profile(env)
    if profile is not None:
        family = getattr(video_codec, "gpu_family", "") or ""
        gpu_enc = profile.encoders.get(family)
        api = profile.api
    else:
        # No GPU profile context (older callers / no probe data): fall
        # back to the profile's NVENC encoder.
        gpu_enc = getattr(video_codec, "gpu_encoder", "") or ""
        api = "nvenc" if gpu_enc else None
    if not gpu_enc:
        return (None, None)
    gpu_info = getattr(env, "gpu", None)
    if gpu_info is not None and gpu_info.functional.get(gpu_enc, False):
        return (gpu_enc, api)
    return (None, None)


class EncoderWorker(QThread):
    log_msg       = Signal(str)
    progress_msg  = Signal(str, int, int)   # (filename, current, total)
    finished_queue = Signal(int, int)        # (success_count, fail_count)

    # v4.4.3/v4.6.0: class-level defaults for attributes normally set in
    # __init__. The mocked test suite builds workers via ``__new__``
    # (bypassing __init__) and calls non-Qt methods directly; without
    # these defaults those instances crash with AttributeError on
    # ``verbose`` / ``_current_total`` (the "AttributeError: no attribute
    # 'verbose'" class of bugs from the v4.4.3 changelog). Instance
    # assignment in __init__ shadows these harmlessly.
    verbose = False
    _current_idx = 0
    _current_total = 0
    _current_filename = ""

    def __init__(
        self,
        in_dir: Path,
        out_dir: Path,
        video_codec: VideoCodecProfile,
        audio_profile: AudioProfile,
        container: ContainerProfile,
        crf: int,
        preset_label: str,
        delete_source: bool,
        env: EnvProbe,
        extensions: set[str],
        resolution: ResolutionProfile,
        audio_level_db: float = 0.0,
        use_ffmpeg_fallback: bool = False,
        subtitle_lang: str | None = None,
        force: bool = False,
        # v4.1.0: explicit overrides for the intelligent worker-count
        # computation. When None, EncoderWorker computes (worker_count,
        # threads_per_worker) from CPU topology so that
        # ``worker_count * threads_per_worker <= logical_threads - 1``
        # (i.e. no thread oversubscription → no hard lock). When set,
        # these take precedence — useful for troubleshooting or for
        # workloads where the auto-compute picks a suboptimal split.
        # Both can also be supplied via env.av1an_flags["max_workers"] /
        # ["threads_per_worker"] (set by the CLI's --max-workers /
        # --threads-per-worker flags) so the GUI doesn't need code changes
        # to honor them.
        max_workers: int | None = None,
        threads_per_worker: int | None = None,
        # v4.6.0: encode engine selection. "auto" uses the NVENC GPU
        # encoder when the selected codec family has one AND the env
        # probe's live encode test proved it works on this system;
        # otherwise (or with "cpu") the CPU encoders are used. "gpu"
        # requests GPU and falls back to CPU with a log line when the
        # hardware is unavailable. Resolved against env.av1an_flags
        # ["engine"] like the other CLI-plumbed flags.
        engine: str | None = None,
        # v4.7.0: hybrid-lane support. *file_subset* restricts this
        # worker to an explicit file list (the hybrid scheduler scans and
        # partitions the queue up front, then spawns a GPU-lane and a
        # CPU-lane worker with disjoint subsets). *lane* suffixes the
        # per-worker temp dir so one lane's cleanup sweep can never
        # delete the other lane's intermediates. *ffmpeg_threads* caps
        # the CPU lane's software ffmpeg encode (GPU jobs are capped by
        # NVENC silicon, not threads).
        file_subset: list[Path] | None = None,
        lane: str = "",
        ffmpeg_threads: int | None = None,
    ):
        super().__init__()
        self.in_dir = in_dir
        self.out_dir = out_dir
        self.video_codec = video_codec
        self.audio_profile = audio_profile
        self.container = container
        self.crf = crf
        self.preset_val = video_codec.preset_map.get(preset_label, 6)
        self.delete_source = delete_source
        self.env = env
        self.extensions = extensions
        self.resolution = resolution
        self.audio_level_db = audio_level_db
        self.use_ffmpeg_fallback = use_ffmpeg_fallback
        self.subtitle_lang = subtitle_lang
        # v5: force=True skips ffprobe validation and attempts encode even
        # for files ffprobe cannot read. Use for the 1% edge case where
        # ffprobe fails but the file is actually valid (rare codec, broken
        # container metadata, etc.). Default False — most "ffprobe can't
        # read" files are genuinely invalid (failed downloads, HTML saved
        # as .mp4, truncated files, etc.).
        self.force = force
        # v4.1.0: intelligent chunking overrides. Falls back to
        # env.av1an_flags if not explicitly passed (so the CLI flags
        # --max-workers / --threads-per-worker reach the GUI-spawned
        # worker without ui_window.py code changes).
        self.max_workers = max_workers if max_workers is not None else (
            env.av1an_flags.get("max_workers") if isinstance(
                env.av1an_flags.get("max_workers"), int
            ) else None
        )
        self.threads_per_worker_override = (
            threads_per_worker if threads_per_worker is not None else (
                env.av1an_flags.get("threads_per_worker") if isinstance(
                    env.av1an_flags.get("threads_per_worker"), int
                ) else None
            )
        )
        # v4.6.0: engine selection ("auto" | "gpu" | "cpu"). Falls back to
        # env.av1an_flags["engine"] when not passed explicitly (same
        # pattern as max_workers — lets the CLI reach the GUI-spawned
        # worker without ui_window changes). Resolved to a concrete
        # GPU/CPU decision in run() via resolve_gpu_encoder().
        self.engine = engine if engine in ("auto", "gpu", "cpu") else (
            env.av1an_flags.get("engine", "auto")
            if env.av1an_flags.get("engine") in ("auto", "gpu", "cpu")
            else "auto"
        )
        # Resolved in run(): NVENC encoder name when the GPU path is
        # active, None for CPU. _ffmpeg_fallback_encode and
        # _prepare_input read this (GPU mode implies the ffmpeg path —
        # av1an cannot drive NVENC — which also means no pre-scale
        # intermediate: the ffmpeg path scales inline).
        self._gpu_encoder: str | None = None
        self._gpu_api: str | None = None
        # v4.2.1: quiet mode by default. Tech-detail log lines (CMD:,
        # live tail of av1an/ffmpeg stderr, DIAGNOSIS blocks, resolution
        # map, pre-flight validation table, heartbeat) are gated behind
        # self.verbose. Default False = only per-file success/fail +
        # final summary. Pass --verbose (or set
        # env.av1an_flags["verbose"]=True) for the full tech dump.
        self.verbose = bool(env.av1an_flags.get("verbose", False))
        # v4.3.0: skip-existing detection. When True (default), the
        # worker probes the output file before encoding; if it already
        # exists with a matching video+audio codec (and matching
        # resolution when scaling was requested), the file is skipped
        # instead of re-encoded. Pass --force-reencode (or set
        # env.av1an_flags["skip_existing"]=False) to disable.
        self.skip_existing = bool(env.av1an_flags.get("skip_existing", True))
        # v4.4.0: per-file encode timeout (seconds). Default 86400s = 24h,
        # up from v4.0.0's 7200s = 2h. A 30GB 1080p BluRay rip at SVT-AV1
        # preset 6 (~5-10 fps) on a 2-hour movie takes 4-10 hours; the old
        # 2h timeout killed massive-file encodes partway through. The STOP
        # button handles user-initiated aborts; this timeout is just a
        # safety net for truly wedged processes. Configurable via --timeout.
        self.encode_timeout = int(env.av1an_flags.get("encode_timeout", 86400))
        # v4.4.4: inline_scale — when True and a target resolution is
        # selected, the scale filter is passed directly to av1an via
        # --ffmpeg-filter-args instead of pre-scaling to a CRF-16
        # intermediate. This skips the extra encode pass entirely (no
        # intermediate file → no disk space wasted) and is significantly
        # faster. The intermediate path remains the default because it
        # works with every chunk method and is robust against av1an/
        # VapourSynth filter-arg quirks on older builds. Toggle via the
        # "Inline scale (no intermediate)" UI checkbox or --inline-scale.
        self.inline_scale = bool(env.av1an_flags.get("inline_scale", False))
        # Stashed per-file by _process_one_file so _encode_one can inject
        # the scale filter into av1an's --ffmpeg-filter-args without
        # changing its (recursively-called) signature.
        self._current_scale_filter: str = ""
        # Resolved at run() time — kept on self so _encode_one can read it
        # without changing its call signature (which is invoked recursively
        # by the y4m-pipe-break retry path).
        self._resolved_threads_per_worker = 0
        self._stop = False
        self._current_temps: list[Path] = []   # temps for the file currently being processed
        self._sources_to_delete: list[Path] = []  # sources deferred for deletion after final cleanup
        self.success_count = 0
        self.fail_count = 0
        # v4.3.0: tracks files skipped because the output already existed
        # with a matching codec. Reported in the final summary as
        # "Skipped: N" alongside Success/Failed.
        self.skipped_count = 0
        # v5-02: track consecutive failures with the same error pattern.
        # After 3 consecutive same-pattern failures, auto-abort the queue.
        self._consecutive_fail_count = 0
        self._last_fail_pattern: str | None = None
        # v3 (OTC-013, SEI CERT FIO09-C): each worker gets its own
        # per-PID subdir under the shared app temp dir, so the final
        # cleanup sweep can safely nuke only this worker's intermediates
        # without affecting a concurrent worker. The subdir is created
        # with mode=0o700 to prevent symlink attacks from other users.
        self.file_subset = file_subset
        self.lane = lane
        self.ffmpeg_threads = ffmpeg_threads
        self._temp_dir = _worker_temp_dir(os.getpid(), lane=lane)
        # v6-06: KeepAwake instance — started in run(), stopped in finally.
        # mouse_nudge defaults to False (opt-in) to avoid surprising the
        # user with cursor movement. systemd-inhibit is always-on when
        # available (no visible side effects).
        self._keepawake = KeepAwake(
            log_fn=lambda msg: self.log_msg.emit(msg),
            enable_mouse_nudge=False,
        )
        self._encode_start_time = 0.0
        # v4.4.0: per-file context for combined status lines. Stashed
        # by _process_one_file so downstream methods can emit
        # "[N/total] filename — STATUS" without changing their signatures.
        self._current_idx = 0
        self._current_total = 0
        self._current_filename = ""

    def _status_prefix(self) -> str:
        """v4.4.0: Build the '[N/total] filename — ' prefix for combined status lines."""
        if self._current_total:
            return f"[{self._current_idx}/{self._current_total}] {self._current_filename} — "
        return f"{self._current_filename} — " if self._current_filename else ""

    def _vlog(self, msg: str) -> None:
        """Verbose-only log emit. No-op unless self.verbose is True.

        v4.2.1: the default log output is quiet — only per-file
        success/fail + final summary. All tech detail (CMD: lines,
        live tail of av1an/ffmpeg stderr, DIAGNOSIS blocks, resolution
        maps, pre-flight validation, heartbeats) goes through _vlog so
        it's suppressed by default. Pass --verbose to see it.
        """
        if self.verbose:
            self.log_msg.emit(msg)

    def _compute_intelligent_worker_count(self) -> tuple[int, int]:
        """Compute ``(worker_count, threads_per_worker)`` to prevent thread
        oversubscription on high-core-count machines.

        PROBLEM (v4.0.0 and earlier)
        ----------------------------
        ``run()`` set ``worker_count = max(1, physical_cores - 1)`` and
        passed no per-chunk thread cap to the encoder. SVT-AV1's default
        ``--threads 0`` means "use all logical cores," so each chunk-parallel
        worker spawned an SvtAv1EncApp process that grabbed every logical
        thread. On a 28-thread Xeon (14 physical cores), 13 workers × 28
        threads = ~364 active threads on 28 logical CPUs — the kernel
        scheduler drowns, I/O wait escalates, and the box hard-locks even
        though no single process is at fault. The 1-second STOP-button
        poll in ``_run_with_stop_check`` can't get scheduled, so even
        clicking STOP doesn't recover it.

        v4.0.0 made it WORSE for the phone-video workload because the
        ``--chunk-method select`` auto-override keeps the pipeline tighter
        (no Hybrid warm-up between chunks), so more SVT-AV1 instances hit
        full tilt at the same instant.

        SOLUTION
        --------
        Budget the total thread count to ``logical_threads - 1`` (one
        logical thread reserved for OS/UI), then split that budget across
        chunk-parallel workers. Each encoder instance gets
        ``--threads N`` so it can't grab more than its share.

        Algorithm
        ---------
        1. ``budget = max(1, logical_threads - 1)`` — leave 1 logical
           thread for OS / UI / av1an orchestrator.
        2. ``ideal_tpw = 4`` — empirical sweet spot for SVT-AV1, x265,
           and vpxenc. Beyond ~6 threads per encoder instance you hit
           memory-bandwidth contention and diminishing returns.
        3. ``target_workers = max(1, budget // ideal_tpw)``.
        4. Cap ``target_workers`` at ``max(1, physical_cores - 1)`` so
           chunk-parallel never exceeds the physical core count.
        5. ``threads_per_worker = max(1, budget // target_workers)``.
        6. Apply user overrides (``self.max_workers`` /
           ``self.threads_per_worker_override``) if provided.

        Examples
        --------
          4-core / 8-thread laptop:
            budget=7, target_workers=7//4=1, tpw=7//1=7  → 1×7 = 7
          8-core / 16-thread desktop:
            budget=15, target_workers=15//4=3, tpw=15//3=5 → 3×5 = 15
          14-core / 28-thread Xeon (the user's box):
            budget=27, target_workers=27//4=6, tpw=27//6=4 → 6×4 = 24
            (leaves 4 logical threads for OS/UI breathing room)
          32-core / 64-thread EPYC:
            budget=63, target_workers=63//4=15, tpw=63//15=4 → 15×4=60
          1-core / 2-thread VM:
            budget=1, target_workers=1, tpw=1 → 1×1 = 1

        Returns ``(worker_count, threads_per_worker)``. Both are ≥1.
        """
        physical = max(1, self.env.cpu.physical_cores)
        logical = max(1, self.env.cpu.logical_threads)

        # User override short-circuit (highest priority).
        if self.max_workers is not None and self.threads_per_worker_override is not None:
            wc = max(1, int(self.max_workers))
            tpw = max(1, int(self.threads_per_worker_override))
            return wc, tpw

        # Budget: leave 1 logical thread for OS / UI / av1an orchestrator.
        budget = max(1, logical - 1)

        # Ideal threads per encoder instance — empirical sweet spot.
        # v4.1.0 used 4; v4.1.1 bumped to 6 because SVT-AV1 with only 4
        # threads was too slow per-chunk, making the total throughput
        # feel "borked" even though the thread budget was correct.
        # With 6 threads per worker, SVT-AV1 has enough parallelism for
        # motion estimation while staying under the logical-thread budget.
        IDEAL_THREADS_PER_WORKER = 6

        # Target worker count from budget / ideal_tpw.
        target_workers = max(1, budget // IDEAL_THREADS_PER_WORKER)

        # Cap at physical_cores - 1 so chunk-parallel doesn't exceed
        # physical core count (avoids L3 cache thrash on chiplet CPUs).
        max_by_phys = max(1, physical - 1) if physical > 1 else 1
        target_workers = min(target_workers, max_by_phys)

        # Apply --max-workers override if provided (still cap by physical).
        if self.max_workers is not None:
            target_workers = min(max(1, int(self.max_workers)), max_by_phys)

        # Compute threads per worker.
        if self.threads_per_worker_override is not None:
            tpw = max(1, int(self.threads_per_worker_override))
        else:
            tpw = max(1, budget // target_workers)

        return target_workers, tpw

    def _run_with_stop_check(
        self,
        cmd: list[str],
        env: dict[str, str] | None = None,
        timeout: int = 7200,
        log_prefix: str = "  ",
    ) -> tuple[str, int, str, str]:
        """Run a subprocess with STOP-button support.

        Replaces ``subprocess.run(cmd, capture_output=True, text=True,
        timeout=7200)`` in the av1an and ffmpeg-fallback encode paths so
        that clicking STOP in the UI interrupts a running encode within
        ~1 second instead of waiting up to 2 hours for the per-file
        timeout to expire.

        Polls ``self._stop`` every ~1 second.  When STOP is requested,
        sends SIGTERM to the subprocess's *process group* (so av1an's
        child encoders — SvtAv1EncApp / vpxenc / x265 — die too, not just
        the av1an parent), waits 5s, then SIGKILLs the group if still
        alive.  Also enforces the overall ``timeout`` (7200s) limit.

        Two background drainer threads read stdout/stderr continuously
        into StringIO buffers.  This prevents the classic pipe-buffer
        deadlock: av1an's progress bar can easily exceed the ~64KB OS
        pipe buffer over a long encode, and without draining the child
        would block on ``write()`` and ``proc.poll()`` would never see
        it exit.  This is the same pattern ``subprocess.run`` uses
        internally via ``_communicate``.

        Returns a 4-tuple ``(status, returncode, stdout, stderr)`` where
        ``status`` is one of:

          - ``"ok"``      — process exited normally; caller inspects
                            ``returncode`` (0 = success) and uses
                            ``stdout`` / ``stderr`` for diagnostics.
          - ``"stop"``    — user requested STOP via the UI.  Caller must
                            NOT increment ``fail_count`` (a user abort is
                            not a transcode failure).  ``self._stop`` is
                            already True (set by the UI thread), so the
                            orchestrator's queue loop will break on the
                            next iteration and emit
                            "STOP: Aborted by user."
          - ``"timeout"`` — process exceeded ``timeout`` seconds.
                            Caller MUST increment ``fail_count`` (a
                            timeout is a failure) and emit the existing
                            user-visible TIMEOUT message.

        Raises ``OSError`` / ``subprocess.SubprocessError`` if the
        ``Popen`` constructor itself fails (e.g. ``FileNotFoundError``
        when the binary is missing) — the caller's existing ``except``
        clause handles these unchanged.
        """
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            # start_new_session=True puts the child in its own process
            # group (setsid).  We can then os.killpg() the whole group
            # to reach av1an's child encoders (SvtAv1EncApp / vpxenc /
            # x265), which a bare proc.terminate() would miss.
            start_new_session=True,
        )

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()

        # v4.1.1: live tail — emit each line of av1an's stdout/stderr
        # to the GUI log as it arrives, so the user sees progress in
        # real-time instead of staring at a frozen "Encoding: file.mp4"
        # message for 10+ minutes. The previous drainer read into a
        # StringIO buffer and only emitted on process exit, which made
        # v4.1.0's slower (capped-thread) encodes look "borked" even
        # though av1an was working fine underneath.
        #
        # Handles both \n (log lines) and \r (progress bar updates) as
        # line boundaries, so av1an's progress bar renders correctly.
        # Incomplete trailing data is buffered until the next read
        # completes the line.
        def _drain(stream, buf, emit_fn, prefix):
            """Read from stream into buf, emitting each complete line via
            emit_fn. Handles \\n and \\r as line boundaries."""
            pending = ""
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        break
                    buf.write(chunk)
                    if emit_fn is None:
                        continue
                    pending += chunk
                    # Emit each complete line (delimited by \n or \r).
                    # av1an's progress bar uses \r; log lines use \n.
                    while True:
                        nl = pending.find('\n')
                        cr = pending.find('\r')
                        if nl == -1 and cr == -1:
                            break
                        if nl == -1:
                            pos = cr
                        elif cr == -1:
                            pos = nl
                        else:
                            pos = min(nl, cr)
                        line = pending[:pos]
                        pending = pending[pos + 1:]
                        stripped = line.rstrip()
                        if stripped:
                            try:
                                emit_fn(f"{prefix}{stripped}")
                            except (RuntimeError, OSError):
                                # Signal might be disconnected mid-encode
                                # if the GUI is closing. Stop emitting
                                # but keep draining the buffer.
                                emit_fn = None
                                break
                    if emit_fn is None:
                        break
            except (OSError, ValueError):
                # Stream closed under us or process gone — stop reading.
                pass
            # Emit any remaining pending data (process exited mid-line).
            if emit_fn is not None:
                stripped = pending.rstrip()
                if stripped:
                    try:
                        emit_fn(f"{prefix}{stripped}")
                    except (RuntimeError, OSError):
                        pass

        tail_prefix = f"{log_prefix}│ "
        # v4.2.1: gate live tail behind self.verbose. Default is quiet —
        # no per-frame ffmpeg/av1an output in the GUI log. The buffer
        # still captures everything for diagnostic purposes (returned
        # to caller as stdout/stderr).
        tail_emit = self.log_msg.emit if self.verbose else None
        t_out = threading.Thread(
            target=_drain,
            args=(proc.stdout, stdout_buf, tail_emit, tail_prefix),
            daemon=True,
        )
        t_err = threading.Thread(
            target=_drain,
            args=(proc.stderr, stderr_buf, tail_emit, tail_prefix),
            daemon=True,
        )
        t_out.start()
        t_err.start()

        status = "ok"
        rc: int | None = None
        start_time = time.monotonic()
        # v4.1.1: heartbeat timer — emit a "still encoding" message every
        # 30 seconds so the user knows the process is alive even if av1an
        # isn't producing line-delimited output (e.g. during a long SVT-AV1
        # encode that only updates a \r progress bar, which the live tail
        # emits as a single line that might not change for minutes).
        last_heartbeat = start_time
        HEARTBEAT_INTERVAL = 30  # seconds
        while True:
            rc = proc.poll()
            if rc is not None:
                # Process exited — break and drain pipes below.
                break

            if self._stop:
                self.log_msg.emit(
                    f"{log_prefix}STOP: Aborting current encode, "
                    f"terminating subprocess..."
                )
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    # Process already gone — nothing to signal.
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # SIGTERM didn't take effect within the grace period —
                    # escalate to SIGKILL on the whole group.
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except (ProcessLookupError, OSError):
                        pass
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        # Truly stuck (e.g. uninterruptible IO).  We've
                        # done what we can; the process will be reaped
                        # later.  Continue to pipe drainage.
                        pass
                status = "stop"
                self.log_msg.emit(f"{log_prefix}STOP: Subprocess terminated.")
                break

            if time.monotonic() - start_time > timeout:
                # Overall timeout — kill the process group.  The caller
                # logs the user-visible TIMEOUT message (it includes the
                # file name / "ffmpeg" context this helper doesn't know).
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                status = "timeout"
                break

            # v4.4.1: heartbeat gated behind --verbose. The user wants
            # just start + finish lines — no "still encoding" chatter
            # in between. If a 10-hour encode looks hung without the
            # heartbeat, they can run with --verbose to see it.
            now = time.monotonic()
            if self.verbose and now - last_heartbeat >= HEARTBEAT_INTERVAL:
                elapsed = int(now - start_time)
                self.log_msg.emit(
                    f"{log_prefix}... {elapsed}s elapsed"
                )
                last_heartbeat = now

            time.sleep(1)

        # Wait for drainer threads to finish reading any remaining pipe
        # data, then close the pipes explicitly (defensive — __del__
        # would also close them, but explicit is better and avoids
        # ResourceWarning under -X dev).
        t_out.join(timeout=10)
        t_err.join(timeout=10)
        try:
            proc.stdout.close()
        except (OSError, ValueError):
            pass
        try:
            proc.stderr.close()
        except (OSError, ValueError):
            pass

        return (
            status,
            rc if rc is not None else -1,
            stdout_buf.getvalue(),
            stderr_buf.getvalue(),
        )

    def _ffmpeg_fallback_encode(
        self,
        file_path: Path,
        encode_input: Path,
        output_f: Path,
    ) -> bool:
        """Encode a single file using pure ffmpeg (av1an fallback path).

        Used when av1an cannot initialize VapourSynth.  No chunk-parallel
        mode, but ffmpeg uses multithreaded encoding internally.

        Returns True on success, False on failure.
        """
        # Check if ffmpeg has the video encoder we need
        # v4.6.0: GPU mode swaps in the hardware encoder and its vargs.
        # v4.8.0: VAAPI/QSV APIs build their command shape (device init,
        # hwupload filter, quality args) from gpu_profiles; NVENC keeps
        # the original profile vargs.
        ffmpeg_enc = self._gpu_encoder or self.video_codec.ffmpeg_encoder
        v_args = (
            (self.video_codec.gpu_vargs_fn or self.video_codec.ffmpeg_vargs_fn)
            if self._gpu_encoder else self.video_codec.ffmpeg_vargs_fn
        )(self.crf, self.preset_val)
        hw_pre_args: list[str] = []
        hw_filter_args: list[str] = []
        if self._gpu_encoder and self._gpu_api in ("vaapi", "qsv"):
            profile = selected_gpu_profile(self.env)
            if profile is not None:
                hw_pre_args = encoder_pre_args(profile)
                hw_filter_args = encoder_filter_chain(profile)
                v_args = encoder_quality_args(
                    self._gpu_api, ffmpeg_enc, self.crf, self.preset_val
                )
        # v3: use the module-level FFMPEG_LIB_KEY_MAP (OTC-007).
        ffmpeg_lib_key = ffmpeg_lib_key_for(ffmpeg_enc)

        if not self.env.ffmpeg_libs.get(ffmpeg_lib_key, False):
            self.log_msg.emit(
                f"  FATAL: ffmpeg does not have '{ffmpeg_enc}' encoder. "
                f"Cannot fall back. Install a ffmpeg build with {ffmpeg_enc} support."
            )
            return False

        # Belt-and-suspenders: if a target resolution is set, inject -vf scale
        # directly into the ffmpeg command. This guarantees the output resolution
        # matches the dropdown even if the intermediate pre-scale was bypassed.
        vf_scale_args: list[str] = []
        if self.resolution.width is not None and self.resolution.height is not None:
            if self._gpu_api == "vaapi":
                # v4.8.0: VAAPI scales ON the hardware — combine the
                # scalar with the hwupload upload in one chain.
                vf_scale_args = [
                    "-vf", (
                        f"scale={self.resolution.width}:{self.resolution.height}:"
                        f"force_original_aspect_ratio=decrease:force_divisible_by=2,"
                        f"format=nv12,hwupload"
                    ),
                ]
                hw_filter_args = []
            else:
                vf_scale_args = [
                    "-vf", (
                        f"scale={self.resolution.width}:{self.resolution.height}:"
                        f"force_original_aspect_ratio=decrease:force_divisible_by=2"
                    ),
                ]

        # Audio args from profile
        audio_args = list(self.audio_profile.params)
        if abs(self.audio_level_db) > 0.01:
            per_file_gain = self._analyze_audio_loudness(file_path)
            if per_file_gain is not None and abs(per_file_gain) > 0.01:
                audio_args.extend(["-af", f"volume={per_file_gain:+.1f}dB"])
            else:
                static_db = f"{self.audio_level_db:+.1f}".replace("+", "")
                audio_args.extend(["-af", f"volume={static_db}dB"])

        # Container-specific muxer flags. -movflags +faststart is MP4-only
        # (it relocates the moov atom for streaming); passing it for MKV or
        # WebM is silently ignored by ffmpeg but pollutes the command line
        # and confuses users reading the log. Apply it only when the
        # output container is MP4.
        mux_flags: list[str] = []
        if self.container.ext == "mp4":
            mux_flags = ["-movflags", "+faststart"]

        cmd = [self.env.ffmpeg_path] + hw_pre_args + [
            "-i", str(encode_input),
        ] + vf_scale_args + hw_filter_args + v_args
        # v4.7.0: CPU lane thread cap in hybrid mode (the GPU lane's
        # NVENC job keeps ~2 threads for decode/mux). Never applied on
        # the GPU path — NVENC throughput is silicon-bound, not
        # thread-bound. Skipped when the cap is 0/unset.
        if self._gpu_encoder is None and self.ffmpeg_threads:
            # v4.7.1: libx265 maps -threads to frame-threads, capped at
            # X265_MAX_FRAME_THREADS (16) — larger values abort the
            # encoder ("frameNumThreads must be [0 .. X265_MAX_FRAME_
            # THREADS)"). SVT-AV1 and libvpx accept the full budget.
            threads = self.ffmpeg_threads
            if ffmpeg_enc == "libx265":
                threads = min(threads, 16)
            cmd += ["-threads", str(threads)]
        cmd += audio_args + mux_flags + [
            "-y",
            str(output_f),
        ]

        try:
            result = self._run_with_stop_check(cmd, timeout=self.encode_timeout, log_prefix="  ")
            status, rc, stdout, stderr = result

            if status == "stop":
                # User requested STOP — do NOT count as failure.  The
                # caller (_process_one_file) guards the fail_count
                # increment with `if not self._stop`.  Remove partial
                # output so it isn't mistaken for a finished file.
                output_f.unlink(missing_ok=True)
                return False
            if status == "timeout":
                self.log_msg.emit(f"{self._status_prefix()}FAIL: timeout (exceeded {self.encode_timeout}s limit)")
                return False

            # status == "ok" — wrap in CompletedProcess so the downstream
            # returncode/stderr logic is byte-for-byte unchanged.
            res = subprocess.CompletedProcess(cmd, rc, stdout, stderr)

            if res.returncode == 0 and output_f.exists():
                src_size = file_path.stat().st_size
                out_size = output_f.stat().st_size
                ratio = out_size / src_size if src_size > 0 else 0

                # Integrity gate: 1KB absolute minimum. A valid container
                # header alone is ~1KB; anything below is definitely corrupt.
                # The duration check in _verify_and_finalize (≥95% of source
                # duration) is the real quality gate for high-bitrate sources.
                if out_size > 1024:
                    return True
                else:
                    self.log_msg.emit(
                        f"  INTEGRITY: output only {ratio * 100:.1f}% of source."
                    )
                    output_f.unlink(missing_ok=True)
                    return False
            else:
                stderr_snip = (res.stderr or "")[-300:]
                self.log_msg.emit(
                    f"  ffmpeg error (rc={res.returncode}): {stderr_snip.strip()}"
                )
                # v4.7.1: remove the partial output. Without this, a
                # failed encode left a truncated file that ffprobe can
                # still parse as the right codec — and skip-existing
                # would then treat it as a finished archive forever.
                output_f.unlink(missing_ok=True)
                return False
        except OSError as e:
            self.log_msg.emit(f"{self._status_prefix()}FAIL: system error: {e}")
            return False

    def run(self):
        # v4.1.0: intelligent worker count + per-chunk thread cap.
        # Replaces the v3 ``max(1, physical_cores - 1)`` heuristic that
        # produced 13 workers × auto (≈28) = 364 threads on a 28-thread
        # Xeon and drowned the kernel scheduler (hard lock).
        # _compute_intelligent_worker_count returns (worker_count,
        # threads_per_worker) such that
        #   worker_count * threads_per_worker <= logical_threads - 1
        # The threads_per_worker is stashed on self so _encode_one can
        # inject it into the encoder's --video-params (each SvtAv1EncApp
        # / vpxenc / x265 instance then respects its share).
        worker_count, threads_per_worker = self._compute_intelligent_worker_count()
        self._resolved_threads_per_worker = threads_per_worker
        phys = self.env.cpu.physical_cores
        logical = self.env.cpu.logical_threads

        # ── v4.6.0: engine resolution (GPU vs CPU) ──
        # GPU mode is a variant of the ffmpeg path: av1an invokes
        # encoder CLI binaries (SvtAv1EncApp / vpxenc / x265) and cannot
        # drive NVENC, so an active GPU encoder forces the single-pass
        # ffmpeg path. NVENC on even a GTX 1070 encodes 1080p at several
        # hundred fps — one ffmpeg process beats av1an's chunk-parallel
        # CPU workers, and chunking becomes unnecessary.
        self._gpu_encoder, self._gpu_api = resolve_gpu_encoder(
            self.engine, self.video_codec, self.env)
        if self._gpu_encoder:
            self.use_ffmpeg_fallback = True
            self.log_msg.emit(
                f"ENGINE: GPU ({self._gpu_encoder}, {self._gpu_api}) — "
                f"single-pass ffmpeg hardware encode; av1an chunk-parallel "
                f"not used."
            )
        elif self.engine == "gpu":
            gpu_enc = getattr(self.video_codec, "gpu_encoder", "") or ""
            if not gpu_enc and not getattr(self.video_codec, "gpu_family", ""):
                self.log_msg.emit(
                    f"ENGINE: GPU requested but {self.video_codec.label} has no "
                    f"hardware encoder — using CPU."
                )
            else:
                gpu = getattr(self.env, "gpu", None)
                detail = gpu.first_failure_detail if gpu is not None else ""
                self.log_msg.emit(
                    f"ENGINE: GPU requested but no usable hardware encoder for "
                    f"{self.video_codec.label} ({detail or 'unavailable'}) — using CPU."
                )

        # Collect all valid files first (for progress tracking)
        # Exclude our own temp intermediates from previous failed runs.
        if self.file_subset is not None:
            # v4.7.0: hybrid lane — the scheduler partitioned the queue.
            all_files = sorted(self.file_subset)
        else:
            all_files = scan_input_files(self.in_dir, self.extensions)
        total = len(all_files)

        if total == 0:
            self.log_msg.emit("INFO: No matching files found in source directory.")
            self.finished_queue.emit(0, 0)
            return

        # ── Mode banner ──
        # v4.2.1: mode banner is verbose-only. The user doesn't need
        # to know the worker math — they just need files to encode.
        # use_ffmpeg_fallback is set by the main thread's pre-flight check.
        if self.verbose:
            if self._gpu_encoder:
                self._vlog(
                    f"GPU encode: {self.video_codec.label} via "
                    f"{self._gpu_encoder} (NVENC), CPU decode"
                )
            elif self.use_ffmpeg_fallback:
                self._vlog(
                    f"FFmpeg fallback: {self.video_codec.ffmpeg_encoder} on {phys} cores "
                    f"(single-pass, no chunk-parallel)"
                )
            else:
                # v4.1.0: show the thread budget so the user can verify the
                # intelligent worker math at a glance. e.g. on a 28-thread Xeon:
                #   "Chunk-parallel: 6 workers × 4 threads = 24 active
                #    (28 logical - 4 reserved for OS/UI)"
                active = worker_count * threads_per_worker
                reserved = logical - active
                self._vlog(
                    f"Chunk-parallel: {worker_count} workers × {threads_per_worker} threads "
                    f"= {active} active "
                    f"({logical} logical - {reserved} reserved for OS/UI)"
                )
                if self.max_workers is not None or self.threads_per_worker_override is not None:
                    self._vlog(
                        f"  (overrides: max_workers={self.max_workers!r}, "
                        f"threads_per_worker={self.threads_per_worker_override!r})"
                    )
        self.log_msg.emit(f"Found {total} file(s) to process.")
        self._vlog(f"Temp dir: {self._temp_dir}")

        # ── Pre-scan: show each file's source → output resolution ──
        # v4.2.1: resolution map is verbose-only.
        needs_scale = (
            self.resolution.width is not None
            and self.resolution.height is not None
        )
        if self.verbose:
            if needs_scale:
                self._vlog(f"Output resolution: {self.resolution.width}x{self.resolution.height} ({self.resolution.aspect_label})")
            else:
                self._vlog("Output resolution: Original (no scaling)")
            self._vlog("─── FILE RESOLUTION MAP ───")
        self._file_res_map: dict[Path, tuple] = {}  # file -> (src_w, src_h, out_w, out_h)
        if self.env.ffprobe_path:
            for f in all_files:
                info = ffprobe_validate(f, self.env.ffprobe_path)
                sw, sh = None, None
                if info:
                    for s in info.get("streams", []):
                        if s.get("codec_type") == "video":
                            sw = int(s.get("width", 0) or 0)
                            sh = int(s.get("height", 0) or 0)
                            break
                if sw and sh:
                    ow, oh = (self.resolution.width, self.resolution.height) if needs_scale else (sw, sh)
                    self._file_res_map[f] = (sw, sh, ow, oh)
                    if self.verbose:
                        arrow = "->" if needs_scale else "="
                        action = "" if needs_scale or sw == ow else " (no change)"
                        self._vlog(f"  {f.name:<40s} {sw:>5}x{sh:<5} {arrow} {ow:>5}x{oh}{action}")
                else:
                    self._file_res_map[f] = (None, None, self.resolution.width if needs_scale else None, self.resolution.height if needs_scale else None)
                    if self.verbose:
                        self._vlog(f"  {f.name:<40s} (unknown resolution)")
        else:
            if self.verbose:
                self._vlog("  (ffprobe unavailable — resolution map skipped)")
        if self.verbose:
            self._vlog("───────────────────────────")

        # ── v5-04: Pre-flight validation pass ──
        # Scan all files with ffprobe BEFORE the encode loop. Report how
        # many are valid vs invalid. This gives the user immediate feedback
        # ("46 files found, 0 valid, 46 invalid") instead of failing one
        # by one over 2 hours. If ALL files are invalid and force=False,
        # abort now — don't waste time entering the encode loop.
        if self.env.ffprobe_path and not self.force:
            valid_count = 0
            invalid_count = 0
            invalid_samples: list[str] = []
            for f in all_files:
                info = ffprobe_validate(f, self.env.ffprobe_path)
                if info is None:
                    invalid_count += 1
                    if len(invalid_samples) < 3:
                        ft = _identify_file_type(f)
                        invalid_samples.append(f"  {f.name}: {ft}" if ft else f"  {f.name}: (file type unknown)")
                else:
                    has_video = any(s.get("codec_type") == "video" for s in info.get("streams", []))
                    duration = float(info.get("format", {}).get("duration", 0))
                    if has_video and duration >= 0.5:
                        valid_count += 1
                    else:
                        invalid_count += 1
                        if len(invalid_samples) < 3:
                            reason = "no video stream" if not has_video else f"too short ({duration:.1f}s)"
                            invalid_samples.append(f"  {f.name}: {reason}")

            # v4.2.1: pre-flight validation table is verbose-only.
            # The ABORT message (when ALL files are invalid) stays loud.
            if self.verbose:
                self._vlog("─── PRE-FLIGHT VALIDATION ───")
                self._vlog(f"  Valid files:   {valid_count}")
                self._vlog(f"  Invalid files: {invalid_count}")
                if invalid_samples:
                    self._vlog(f"  First {len(invalid_samples)} invalid:")
                    for s in invalid_samples:
                        self._vlog(s)
                self._vlog("─────────────────────────────")

            if valid_count == 0 and invalid_count > 0:
                self.log_msg.emit("")
                self.log_msg.emit(
                    f"ABORT: All {invalid_count} file(s) are invalid. "
                    f"Aborting queue — no files to encode."
                )
                self._vlog(
                    "  Common causes: (1) failed yt-dlp downloads (HTML saved as .mp4), "
                    "(2) files on a network mount that's not responding, "
                    "(3) wrong input directory."
                )
                self._vlog(
                    "  Run `file <filename>` on any file to see what it actually is."
                )
                self.fail_count = invalid_count
                self._final_cleanup_sweep()
                self.log_msg.emit(
                    f"QUEUE COMPLETE. Success: 0, Failed: {self.fail_count}."
                )
                self.finished_queue.emit(0, self.fail_count)
                return
            elif invalid_count > 0:
                # v4.2.1: keep the one-line skip notice (user-facing) but
                # drop the empty line — it just wastes vertical space.
                self.log_msg.emit(
                    f"  {invalid_count} invalid file(s) will be skipped."
                )

        scale_filter = (
            f"scale={self.resolution.width}:{self.resolution.height}:"
            f"force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"pad={self.resolution.width}:{self.resolution.height}:(ow-iw)/2:(oh-ih)/2"
        ) if needs_scale else ""

        # v6-06: Start keep-awake (systemd-inhibit + optional mouse nudge)
        self._encode_start_time = time.monotonic()
        self._keepawake.start()
        try:
            for idx, file_path in enumerate(all_files, 1):
                if self._stop:
                    self.log_msg.emit("STOP: Aborted by user.")
                    break

                prev_success = self.success_count
                prev_fail = self.fail_count

                # v6-06: Update keep-awake ETA before each file.
                # ETA = (avg time per file so far) × (remaining files)
                processed = idx - 1
                if processed > 0:
                    elapsed = time.monotonic() - self._encode_start_time
                    avg_per_file = elapsed / processed
                    remaining = total - processed
                    self._keepawake.update_eta(avg_per_file * remaining)
                else:
                    self._keepawake.update_eta(None)  # unknown for first file

                self._process_one_file(file_path, idx, total, worker_count, needs_scale, scale_filter)

                # v5-02: track consecutive failures with the same error pattern.
                # After 3 consecutive same-pattern failures, auto-abort the queue.
                if self.fail_count > prev_fail:
                    # This file failed — extract the failure pattern from the
                    # last log message (the DIAGNOSIS line or the FAIL line).
                    # We use the first 80 chars as a coarse pattern fingerprint.
                    # If the pattern matches the previous failure, increment the
                    # consecutive counter; otherwise reset it.
                    # (We can't access the log messages directly from here, so
                    # we use a simpler heuristic: if the fail count increased
                    # and the success count didn't, it's a failure. The pattern
                    # is tracked via _last_fail_pattern set in _encode_one.)
                    pass  # pattern tracking is handled in _process_one_file
                elif self.success_count > prev_success:
                    # Success resets the consecutive failure counter.
                    self._consecutive_fail_count = 0
                    self._last_fail_pattern = None

            # ── Final cleanup pass: residual sweep ──
            self._final_cleanup_sweep()

            # ── Deferred source deletion (only after all cleanup is done) ──
            if self._sources_to_delete:
                deleted = 0
                for src in self._sources_to_delete:
                    try:
                        if src.exists():
                            src.unlink()
                            deleted += 1
                    except OSError:
                        # Best-effort: a single un-deletable source must not abort
                        # the rest of the deferred-deletion sweep.
                        pass
                self.log_msg.emit(f"CLEANED: Removed {deleted} source file(s).")
                self._sources_to_delete.clear()

            # v4.2.1: single final summary line — no mode prefix.
            # v4.3.0: include skipped count when > 0.
            if self.skipped_count > 0:
                self.log_msg.emit(
                    f"QUEUE COMPLETE. Success: {self.success_count}, "
                    f"Failed: {self.fail_count}, Skipped: {self.skipped_count}."
                )
            else:
                self.log_msg.emit(
                    f"QUEUE COMPLETE. Success: {self.success_count}, Failed: {self.fail_count}."
                )
            self.finished_queue.emit(self.success_count, self.fail_count)
        finally:
            # v6-06: Always stop keep-awake, even if the encode loop crashed.
            self._keepawake.stop()

    def _process_one_file(self, file_path, idx, total, worker_count, needs_scale, scale_filter):
        """Process a single file end-to-end (validate -> prepare -> encode -> verify).

        Extracted from run() so the per-file control flow is readable.  All
        `continue` statements from the original loop become early `return`s
        here.  The caller (run) simply iterates and re-checks `self._stop` at
        the top of each iteration.

        v4.4.0: the per-file banner is NOT emitted upfront. Instead, each
        terminal status (SKIP / OK / FAIL) emits a SINGLE combined line:
          [N/total] filename — SKIP (already av1/opus)
          [N/total] filename — OK: 1.6MB -> 1.3MB (81%)
          [N/total] filename — FAIL: <reason>
        This halves the log line count for skipped files and makes the
        status visible at a glance without scrolling. The heartbeat
        (every 30s) is the only thing emitted mid-encode.
        """
        self.progress_msg.emit(file_path.name, idx, total)
        # v4.4.0: stash idx/total on self so downstream methods
        # (_verify_and_finalize, _encode_one) can emit combined status
        # lines with the [N/total] filename prefix without changing
        # their call signatures.
        self._current_idx = idx
        self._current_total = total
        self._current_filename = file_path.name
        # v4.4.4: stash scale_filter so _encode_one can inject it into
        # av1an's --ffmpeg-filter-args when self.inline_scale is True.
        self._current_scale_filter = scale_filter or ""

        # --- ffprobe pre-validation ---
        # _validate_file uses _status_prefix() for combined log lines,
        # which reads self._current_idx/_current_total set above.
        skip, info, src_w, src_h = self._validate_file(file_path)
        if skip:
            # v5-02: a skip is a failure for consecutive-failure tracking.
            self._check_consecutive_failures(file_path, accepted=False)
            return  # _validate_file already logged SKIP + incremented fail_count

        # --- Determine actual output resolution ---
        # v4.2.1: source/output resolution is verbose-only.
        if self.verbose and src_w and src_h:
            out_w, out_h = src_w, src_h
            if needs_scale:
                out_w, out_h = self.resolution.width, self.resolution.height
            self._vlog(f"  Source: {src_w}x{src_h}  ->  Output: {out_w}x{out_h}")
        elif self.verbose:
            if needs_scale:
                self._vlog(f"  Source: unknown  ->  Output: {self.resolution.width}x{self.resolution.height}")
            else:
                self._vlog(f"  Source: unknown  ->  Output: original")

        # --- Pre-scale / symlink + build output path ---
        prepared = self._prepare_input(file_path, src_w, src_h, needs_scale, scale_filter)
        if prepared is None:
            # v5-02: prepare failure counts for consecutive-failure tracking.
            self._check_consecutive_failures(file_path, accepted=False)
            return  # _prepare_input already logged + cleaned up + incremented fail_count
        encode_input, output_f = prepared

        # v4.3.0: skip-existing detection. If the output file already
        # exists with a matching video+audio codec (and matching
        # resolution when scaling was requested), skip the encode
        # entirely. This is the default (--skip-existing); pass
        # --force-reencode to disable. A skip is NOT a failure — it's
        # treated as a successful no-op and tracked in skipped_count.
        # v4.4.0: moved ABOVE the disk-space check so skipped files
        # don't trigger disk-space warnings. A skipped file writes
        # nothing to disk, so warning about free space for it is noise
        # that buries the SKIP status the user actually needs to see.
        # Also: combined into a single log line with the [N/total] prefix.
        if self.skip_existing and self._output_already_encoded(file_path, output_f):
            self.skipped_count += 1
            vcodec = self.video_codec.ffprobe_codec_name or "?"
            acodec = self.audio_profile.ffprobe_codec_name or "?"
            self.log_msg.emit(
                f"[{idx}/{total}] {file_path.name} — SKIP (already {vcodec}/{acodec})"
            )
            # v5-02: a skip counts as a success for consecutive-failure
            # tracking — it's not a failure, and the queue shouldn't
            # auto-abort on a run of skips.
            self._check_consecutive_failures(file_path, accepted=True)
            # Clean up any temps _prepare_input may have created (symlinks
            # for av1an, pre-scaled intermediates). The output file
            # itself is NOT touched.
            self._cleanup_current_temps()
            # Defer source deletion if requested — a skip is a successful
            # transcode from the user's perspective (the output exists
            # and matches their codec selection).
            if self.delete_source:
                self._sources_to_delete.append(file_path)
            return

        # v4.4.0: disk space pre-check for massive files. Warns (does NOT
        # abort) if free space on the output/temp partition is less than
        # the source size. Skipped for files < 1 GB. Runs ONLY for files
        # we're actually about to encode (after the skip-existing check).
        self._check_disk_space(file_path, output_f, needs_scale)

        # v4.4.0: emit the per-file banner HERE (not at the top of
        # _process_one_file) so skipped files don't get a dangling
        # "[N/total] filename" line with no status. The heartbeat will
        # fire during the encode to show progress. The final OK/FAIL
        # status line at the end of the encode will repeat the prefix,
        # but that's fine — it's how the user matches status to file.
        self.log_msg.emit(f"[{idx}/{total}] {file_path.name}")

        # --- Encode ---
        encode_ok = self._encode_one(file_path, encode_input, output_f, worker_count)
        if not encode_ok:
            # ffmpeg fallback path: _ffmpeg_fallback_encode does NOT touch
            # _current_temps or fail_count, so we do both here to match the
            # original `else: self.fail_count += 1; self._cleanup_current_temps()`.
            # av1an path: _encode_one's `finally` already cleaned temps and
            # fail_count was incremented inside _encode_one.
            #
            # STOP exception: when the user clicked STOP mid-encode,
            # _run_with_stop_check returned "stop" and _ffmpeg_fallback_encode
            # returned False WITHOUT incrementing fail_count (a user abort is
            # not a transcode failure).  Honor that here by skipping the
            # fail_count increment when self._stop is set — temp cleanup
            # still runs so we don't leak intermediate files.
            if self.use_ffmpeg_fallback:
                if not self._stop:
                    self.fail_count += 1
                self._cleanup_current_temps()
            # v5-02: encode failure counts for consecutive-failure tracking
            # (but only if not a user STOP — a STOP is not a failure).
            if not self._stop:
                self._check_consecutive_failures(file_path, accepted=False)
            return

        # --- Post-encode verification + finalize ---
        accepted = self._verify_and_finalize(file_path, output_f, encode_input, needs_scale)
        if self.use_ffmpeg_fallback:
            # ffmpeg path always cleans up explicitly at every exit
            # (av1an path already cleaned up via _encode_one's `finally`).
            self._cleanup_current_temps()
        # accepted=True  -> success_count already incremented in _verify_and_finalize.
        # accepted=False -> fail_count already incremented + output unlinked there.

        # v5-02: check for consecutive failures with the same pattern.
        self._check_consecutive_failures(file_path, accepted)

    def _check_consecutive_failures(self, file_path: Path, accepted: bool):
        """v5-02: Track consecutive failures and auto-abort after 3.

        After 3 consecutive failures (regardless of pattern — if 3 files
        in a row fail, something is systematically wrong), auto-abort the
        queue with a clear message. The user can still click STOP to
        abort earlier.

        This prevents the scenario from the user's log: 46 files, all
        failing identically, processed one by one over ~2 hours. With
        this fix, the queue aborts after file 3.
        """
        if accepted:
            self._consecutive_fail_count = 0
            return

        self._consecutive_fail_count += 1
        if self._consecutive_fail_count >= 3 and not self._stop:
            self.log_msg.emit("")
            self.log_msg.emit(
                f"ABORT: {self._consecutive_fail_count} consecutive failures. "
                f"Auto-aborting queue — something is systematically wrong."
            )
            self.log_msg.emit(
                "  The remaining files will likely fail the same way. "
                "Fix the root cause (check the diagnostics above) and retry."
            )
            self.log_msg.emit(
                "  Common root causes: (1) all files are invalid (failed downloads), "
                "(2) av1an/encoder binary is broken, (3) out of disk space, "
                "(4) network mount is down."
            )
            self._stop = True

    def _validate_file(self, file_path):
        """ffprobe pre-validation. Returns (skip, info, src_w, src_h).

        skip=True signals the caller to abandon this file — the SKIP log
        line and fail_count increment have already happened here.

        If ffprobe cannot read the file, SKIP it. The encode fails ~99%
        of the time when ffprobe fails (failed download, HTML saved as
        .mp4, truncated, etc.). The `force=True` constructor flag
        overrides this for the rare edge case (rare codec, broken
        container metadata where ffprobe fails but ffmpeg can decode).
        """
        # Reuse pre-scanned dimensions if available, otherwise probe now
        prescan = self._file_res_map.get(file_path)
        src_w, src_h = (prescan[0], prescan[1]) if prescan else (None, None)
        info = None
        if self.env.ffprobe_path:
            info = ffprobe_validate(file_path, self.env.ffprobe_path)
        if info is None:
            if self.force:
                # v4.2.1: verbose-only
                self._vlog(
                    f"WARN: ffprobe could not read {file_path.name} — "
                    f"attempting encode anyway (force=True)."
                )
            else:
                # Identify the file's actual type via `file` command. This
                # reveals "HTML document" (failed yt-dlp download) vs "data"
                # (truncated/encrypted) vs "ISO Media" (valid MP4 that ffprobe
                # just can't parse). Combined single-line status with prefix.
                file_type = _identify_file_type(file_path)
                self.log_msg.emit(f"{self._status_prefix()}SKIP: not a valid video (ffprobe could not read it)")
                if file_type and self.verbose:
                    self._vlog(f"  File type: {file_type}")
                    if "HTML" in file_type or "ASCII" in file_type or "text" in file_type:
                        self._vlog(
                            "  This looks like a text/HTML file, not a video. "
                            "Common cause: failed yt-dlp download (region-locked, "
                            "age-restricted, or removed video). Re-download the file."
                        )
                    elif "data" in file_type:
                        self._vlog(
                            "  File type is 'data' — possibly truncated, encrypted, "
                            "or a partial download. Verify the file plays in mpv/VLC."
                        )
                if self.verbose:
                    self._vlog(
                        "  (Use the Force checkbox to attempt encode anyway.)"
                    )
                self.fail_count += 1
                return (True, None, None, None)
        else:
            duration = float(info.get("format", {}).get("duration", 0))
            has_video = any(s.get("codec_type") == "video" for s in info.get("streams", []))
            if not has_video:
                self.log_msg.emit(f"{self._status_prefix()}SKIP: no video stream")
                self.fail_count += 1
                return (True, None, None, None)
            if duration < 0.5:
                self.log_msg.emit(f"{self._status_prefix()}SKIP: too short ({duration:.1f}s)")
                self.fail_count += 1
                return (True, None, None, None)
            # Extract dims if pre-scan didn't have them
            if not src_w or not src_h:
                for s in info.get("streams", []):
                    if s.get("codec_type") == "video":
                        src_w = int(s.get("width", 0) or 0)
                        src_h = int(s.get("height", 0) or 0)
                        break
        return (False, info, src_w, src_h)

    def _prepare_input(self, file_path, src_w, src_h, needs_scale, scale_filter):
        """Pre-scale (if needed) and ensure the av1an work dir lands in temp.

        Returns (encode_input, output_f) on success, or None on failure
        (after logging + cleaning up current temps + incrementing fail_count).
        """
        # --- Pre-scale with ffmpeg if target resolution selected ---
        # ALL intermediates (scaled files, av1an work dirs) go to the app
        # temp directory so the user's video folders stay clean.
        encode_input = file_path

        # v4.6.0: pre-scale ONLY on the av1an path. The pure-ffmpeg path
        # (the default, and the only path NVENC can run on) scales inline
        # via the -vf args in _ffmpeg_fallback_encode — the intermediate
        # existed solely because VapourSynth source plugins choke on some
        # inputs ffmpeg handles fine. Skipping it on the ffmpeg path
        # removes the entire class of large-file failures: no 0.5-0.8x
        # source-size temp file, no extra full encode pass, and no
        # pre-scale timeout on long/high-bitrate sources.
        if needs_scale and not self.inline_scale and not self.use_ffmpeg_fallback:
            try:
                temp_scaled = _temp_path_for(file_path, ".scaled_tmp.mkv", worker_dir=self._temp_dir)
                self._current_temps.append(temp_scaled)
                # Use libx265 CRF 16 (visually lossless) for the intermediate —
                # NOT ffv1 (unsupported by VapourSynth source plugins: bestsource,
                # ffms2, lsmash — produces empty pipe → "Fatal: Failed to open
                # input file") and NOT CRF 0 (mathematically lossless → 2-4×
                # source size; a 20GB BluRay rip produced a 60-80GB intermediate
                # and crashed the encode with disk-exhaustion errors that
                # presented as cryptic "ffmpeg error (rc=234)" messages).
                # CRF 16 is transparent for archival purposes (~0.5-0.8× source
                # size) and HEVC-in-MKV is universally supported by every VS plugin.
                # For an even faster path that skips the intermediate entirely,
                # see the inline_scale option (passes scale filter to av1an via
                # --ffmpeg-filter-args).
                scale_cmd = [
                    self.env.ffmpeg_path,
                    "-i", str(file_path),
                    "-vf", scale_filter,
                    "-c:v", "libx265",
                    "-crf", "16",            # visually lossless — was 0 (mathematically lossless)
                    "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p",   # force 8-bit 4:2:0
                    "-y",
                    str(temp_scaled),
                ]
                # v4.2.1: Scaling notice is verbose-only.
                self._vlog(f"  Scaling {src_w or '?'}x{src_h or '?'} -> {self.resolution.width}x{self.resolution.height}...")
                # v4.6.0: run via _run_with_stop_check with the full
                # per-file timeout. The old flat
                # subprocess.run(timeout=1800) killed pre-scaling of
                # long/high-bitrate sources at exactly 30 minutes
                # ("FAIL: pre-scale error: Command ... timed out") — a
                # guaranteed large-file failure that also ignored the
                # STOP button for the whole intermediate pass.
                scale_status, scale_rc, _s_out, scale_err = self._run_with_stop_check(
                    scale_cmd, timeout=self.encode_timeout, log_prefix="  ",
                )
                if scale_status == "stop":
                    # User aborted — clean up the partial intermediate and
                    # bail WITHOUT counting a failure (a STOP is not an
                    # encode failure; the queue loop breaks next iteration).
                    self._cleanup_current_temps()
                    return None
                if scale_status == "timeout":
                    self.log_msg.emit(
                        f"{self._status_prefix()}FAIL: pre-scale timeout "
                        f"(exceeded {self.encode_timeout}s limit)"
                    )
                    temp_scaled.unlink(missing_ok=True)
                    self._cleanup_current_temps()
                    self.fail_count += 1
                    return None
                if scale_status == "ok" and scale_rc == 0 and temp_scaled.exists():
                    encode_input = temp_scaled
                    scaled_size = temp_scaled.stat().st_size / 1_048_576
                    # v4.2.1: verbose-only
                    self._vlog(f"  Pre-scale OK ({scaled_size:.1f} MB intermediate)")
                else:
                    stderr_snip = (scale_err or "")[-200:]
                    # v4.2.1: keep user-facing FAIL but shorten; stderr verbose-only
                    self.log_msg.emit(
                        f"{self._status_prefix()}FAIL: pre-scale failed (rc={scale_rc})"
                    )
                    if stderr_snip.strip():
                        self._vlog(f"  ffmpeg stderr: {stderr_snip.strip()}")
                    temp_scaled.unlink(missing_ok=True)
                    self._cleanup_current_temps()
                    self.fail_count += 1
                    return None
            except (OSError, subprocess.SubprocessError) as e:
                # v4.2.1: keep user-facing but shorten
                self.log_msg.emit(f"{self._status_prefix()}FAIL: pre-scale error: {e}")
                self._cleanup_current_temps()
                self.fail_count += 1
                return None

        # --- Ensure av1an work dir lands in the temp directory ---
        # av1an creates its work dir as {input_path}.av1an by default.
        # We do NOT use av1an's --temp flag because it causes "Error: End of file"
        # during scene detection when the input file is in the same directory
        # as --temp (av1an 0.5.2-unstable).  Instead, we ensure the -i argument
        # always points into the temp dir (pre-scaled files already live there;
        # for no-scale we create a symlink).
        if not encode_input.is_relative_to(self._temp_dir):
            symlink_path = _temp_path_for(file_path, encode_input.suffix, worker_dir=self._temp_dir)
            try:
                symlink_path.unlink(missing_ok=True)
                symlink_path.symlink_to(file_path.resolve())
                self._current_temps.append(symlink_path)
                encode_input = symlink_path
            except OSError as e:
                self.log_msg.emit(
                    f"  WARN: Could not create symlink in temp dir: {e}. "
                    f"av1an work dir will be created next to source file."
                )
        # Track the work dir where av1an will actually create it
        av1an_work = Path(f"{encode_input}.av1an")
        self._current_temps.append(av1an_work)

        # --- Build output path (preserve directory structure) ---
        rel_path = file_path.relative_to(self.in_dir)
        target_dir = self.out_dir / rel_path.parent
        target_dir.mkdir(parents=True, exist_ok=True)

        ext = self.container.ext
        # Always add resolution suffix when a target resolution is selected
        res_suffix = f"_{self.resolution.width}x{self.resolution.height}" if needs_scale else ""
        output_f = target_dir / f"{file_path.stem}{res_suffix}_archived.{ext}"

        return (encode_input, output_f)

    def _check_disk_space(self, file_path: Path, output_f: Path, needs_scale: bool) -> None:
        """v4.4.0: Warn if free disk space is less than the encode will need.

        v4.6.0: SEVERE warnings (free space below the source size on the
        partition we're about to write a big intermediate/output to) are
        now USER-FACING — they were verbose-only, so in the default quiet
        mode a batch that was going to die with "No space left on device"
        partway through gave zero advance notice. That silent failure was
        one of the "large files just fail" reports: small files fit in
        the remaining space, big ones didn't. Marginal advice (the 2-3x
        intermediate estimate) stays verbose-only.
        """
        try:
            src_size = file_path.stat().st_size
        except OSError:
            return  # can't stat source — skip the check
        if src_size < 1_073_741_824:  # < 1 GB — skip check for small files
            return
        src_gb = src_size / 1_073_741_824
        # Check output partition — severe when free < source size
        # (the encoded output is usually smaller, but the ffmpeg/av1an
        # buffer cache plus a same-partition temp can eat the difference).
        try:
            out_usage = shutil.disk_usage(output_f.parent)
            out_free_gb = out_usage.free / 1_073_741_824
            if out_free_gb < src_gb:
                self.log_msg.emit(
                    f"  WARN: low disk space on output ({out_free_gb:.1f} GB free, "
                    f"source is {src_gb:.1f} GB) — encode may fail partway through"
                )
            elif self.verbose and out_free_gb < src_gb * 2:
                self._vlog(
                    f"  WARN: output space getting tight ({out_free_gb:.1f} GB free, "
                    f"source is {src_gb:.1f} GB)"
                )
        except OSError:
            pass  # can't check — skip
        # When scaling on the av1an path, also check the temp partition
        # (the CRF-16 intermediate can be ~1x source size). The ffmpeg
        # path scales inline (no intermediate), so no temp warning there.
        if needs_scale and not self.use_ffmpeg_fallback:
            try:
                tmp_usage = shutil.disk_usage(self._temp_dir)
                tmp_free_gb = tmp_usage.free / 1_073_741_824
                # Severe: free temp space below the source size means the
                # intermediate will likely not fit → user-facing warning.
                if tmp_free_gb < src_gb:
                    self.log_msg.emit(
                        f"  WARN: low disk space on temp ({tmp_free_gb:.1f} GB free, "
                        f"source is {src_gb:.1f} GB) — the scale intermediate "
                        f"may not fit. Free space or enable 'Inline scale'."
                    )
                elif self.verbose and tmp_free_gb < src_gb * 2:
                    self._vlog(
                        f"  WARN: temp space getting tight ({tmp_free_gb:.1f} GB free, "
                        f"lossless intermediate may need ~{src_gb * 2:.1f} GB) — "
                        f"consider scaling to a smaller resolution or freeing space"
                    )
            except OSError:
                pass

    def _output_already_encoded(self, file_path: Path, output_f: Path) -> bool:
        """v4.3.0: Check if output_f already exists with a matching codec.

        Returns True (skip the encode) when ALL of the following hold:
          - output_f exists on disk
          - ffprobe can read it (not corrupt)
          - video stream codec_name matches self.video_codec.ffprobe_codec_name
          - audio stream codec_name matches self.audio_profile.ffprobe_codec_name
            (when both the profile and the file have an audio stream)
          - if scaling was requested, output resolution matches the target

        Returns False (proceed with encode) otherwise — including when
        ffprobe is unavailable, the file is unreadable, or any codec
        mismatch is detected. In the False cases, the encode will
        overwrite the existing output (treats it as stale/corrupt).

        CRF/preset are NOT verified because they're encoder settings
        not reliably stored in container metadata. The user must use
        --force-reencode if they want to re-encode at a different CRF
        with the same codec.
        """
        if not output_f.exists():
            return False
        if not self.env.ffprobe_path:
            # Can't verify codec — be safe and re-encode.
            return False
        info = ffprobe_validate(output_f, self.env.ffprobe_path)
        if info is None:
            # File exists but unreadable — treat as needing re-encode.
            return False
        streams = info.get("streams", [])
        vstream = next((s for s in streams if s.get("codec_type") == "video"), None)
        astream = next((s for s in streams if s.get("codec_type") == "audio"), None)
        if not vstream:
            return False
        # Video codec check.
        expected_v = self.video_codec.ffprobe_codec_name
        if expected_v and vstream.get("codec_name") != expected_v:
            return False
        # Audio codec check (only if both profile and file have audio).
        expected_a = self.audio_profile.ffprobe_codec_name
        if expected_a and astream:
            if astream.get("codec_name") != expected_a:
                return False
        # Resolution check (only when scaling was requested).
        if self.resolution.width is not None and self.resolution.height is not None:
            actual_w = int(vstream.get("width", 0) or 0)
            actual_h = int(vstream.get("height", 0) or 0)
            if actual_w != self.resolution.width or actual_h != self.resolution.height:
                return False
        return True

    def _can_ffmpeg_fallback(self) -> bool:
        """v6-01: Check if ffmpeg has the encoder for this codec.

        Returns True if ffmpeg can encode with this codec's ffmpeg_encoder
        (e.g. libsvtav1, libvpx-vp9, libx265), False otherwise.
        Used to decide whether to retry a failed av1an encode with ffmpeg.
        """
        ffmpeg_enc = self.video_codec.ffmpeg_encoder
        lib_key = ffmpeg_lib_key_for(ffmpeg_enc)
        return bool(self.env.ffmpeg_libs.get(lib_key, False))

    def _encode_one(self, file_path, encode_input, output_f, worker_count,
                    chunk_method=None):
        """Dispatch to ffmpeg fallback or av1an. Returns True if encode succeeded.

        ffmpeg fallback: delegates to _ffmpeg_fallback_encode (which itself
        performs the size >=5% integrity check and unlinks bad output).  No
        temp cleanup or fail_count increment happens here for this path —
        _process_one_file handles both at the call site, matching the original.

        av1an: builds and runs the av1an command, performs the size >=5% check
        inline, and wraps everything in try/except/finally so temps are always
        cleaned up via _cleanup_current_temps() — matching the original.  On
        every failure path here, fail_count is incremented inside this method.

        v4.0.0: *chunk_method* is an explicit override used by the y4m-pipe-break
        retry path. When None, the method falls back to
        ``env.av1an_flags["chunk_method_override"]`` (set by env_probe or by
        a previous retry) or av1an's auto-selection. When av1an fails with the
        "Failed to read y4m frame delimiter" pattern (Hybrid chunk method on
        phone-recorded MP4s with sparse keyframes), this method recursively
        retries with ``chunk_method="select"`` and caches that choice so
        subsequent files skip the wasted first attempt.
        """
        # ── Choose encode path: av1an or ffmpeg fallback ──
        if self.use_ffmpeg_fallback:
            # ── Pure ffmpeg encode path ──
            # v4.2.1: Mode banner is verbose-only.
            self._vlog(f"  Mode: ffmpeg ({self.video_codec.ffmpeg_encoder})")
            return self._ffmpeg_fallback_encode(
                file_path, encode_input, output_f,
            )

        # ── av1an encode path (original) ──
        # Resolve encoder name with probe data
        enc = self.video_codec.av1an_encoder
        if enc in ("svt_av1", "svt") and "svt_name" in self.env.av1an_flags:
            enc = self.env.av1an_flags["svt_name"]

        # Build params via config table (no if/else).
        # v4.1.2: do NOT inject --threads into av1an's --video-params.
        # SvtAv1EncApp (the standalone CLI av1an invokes per-chunk) does
        # not accept --threads — only --lp (logical processors). Injecting
        # --threads produced "Unprocessed tokens: --threads" → every
        # chunk failed 3x → no av1an output. Thread capping is done via
        # av1an's --workers flag (chunk-parallel count) and via -threads
        # in the ffmpeg fallback path (where libsvtav1 is a library).
        v_params = self.video_codec.params_fn(self.crf, self.preset_val)

        # Audio params: dual-pass normalization per file, or simple volume
        audio_parts = list(self.audio_profile.params)
        if abs(self.audio_level_db) > 0.01:
            per_file_gain = self._analyze_audio_loudness(file_path)
            if per_file_gain is not None and abs(per_file_gain) > 0.01:
                audio_parts.extend(["-af", f"volume={per_file_gain:+.1f}dB"])
            else:
                # Fallback to knob's static value if analysis failed
                static_db = f"{self.audio_level_db:+.1f}".replace("+", "")
                audio_parts.extend(["-af", f"volume={static_db}dB"])
                # v4.2.1: verbose-only
                self._vlog(f"  Audio: static gain {self.audio_level_db:+.1f} dB (analysis unavailable)")

        audio_str = " ".join(audio_parts)

        cmd = [
            self.env.av1an_path,
            "-i", str(encode_input),
            self.env.av1an_flags.get("worker", "--workers"), str(worker_count),
        ]

        # Chunk method: explicit arg (retry) > env override > av1an auto.
        # v4.0.0: when av1an auto-selects Hybrid (default when no VS source
        # plugins are installed), phone-recorded MP4s with sparse keyframes
        # fail with "Failed to read y4m frame delimiter". The retry path
        # passes chunk_method="select" which uses VapourSynth's select()
        # filter — slower but reliable.
        effective_chunk_method = (
            chunk_method
            or self.env.av1an_flags.get("chunk_method_override")
        )
        if effective_chunk_method:
            cmd.extend(["--chunk-method", effective_chunk_method])
        # v4.2.1: chunk-method banner is verbose-only.
        self._vlog(
            f"  Chunking: {effective_chunk_method or 'auto'} "
            f"(av1an default if no override)"
        )

        # v4.4.4: inline scale — when enabled and a target resolution is
        # selected, pass the scale/pad filter chain directly to av1an via
        # --ffmpeg-filter-args. This skips the CRF-16 intermediate encode
        # entirely (zero temp disk usage for the scaling step) at the cost
        # of running the filter on every chunk-extraction pass. The
        # default (inline_scale=False) uses the pre-scale intermediate,
        # which is more robust across av1an/VapourSynth versions but
        # requires the extra encode pass and 0.5-0.8× source size of temp
        # disk space for the intermediate.
        if self.inline_scale and self._current_scale_filter:
            cmd.extend(["--ffmpeg-filter-args", self._current_scale_filter])
            self._vlog(
                f"  Inline scale: enabled (filter passed via "
                f"--ffmpeg-filter-args, no intermediate file)"
            )

        cmd.extend([
            "--encoder", enc,
            self.env.av1an_flags.get("video_params", "--video-params"), v_params,
            self.env.av1an_flags.get("audio_params", "--audio-params"), audio_str,
            "--concat", self.env.av1an_flags.get("concat_method", "ffmpeg"),
            "-o", str(output_f),
        ])

        # v4.2.1: CMD: line is verbose-only (debugging).
        self._vlog(f"  CMD: {' '.join(cmd)}")

        try:
            result = self._run_with_stop_check(
                cmd, env=_av1an_env(), timeout=self.encode_timeout, log_prefix="  ",
            )
            status, rc, stdout, stderr = result

            if status == "stop":
                # User requested STOP — do NOT increment fail_count (the
                # user explicitly chose to abort, it isn't a transcode
                # failure).  Remove partial output.  self._stop is already
                # True (set by the UI thread), so the orchestrator's
                # queue loop will break on the next iteration and emit
                # "STOP: Aborted by user."
                output_f.unlink(missing_ok=True)
                return False
            if status == "timeout":
                self.fail_count += 1
                # v4.2.1: keep user-facing timeout message but shorten it.
                self.log_msg.emit(f"{self._status_prefix()}FAIL: timeout (exceeded {self.encode_timeout}s limit)")
                return False

            # status == "ok" — wrap in CompletedProcess so the downstream
            # returncode check, diagnostic dump, and pattern matching are
            # byte-for-byte unchanged.
            res = subprocess.CompletedProcess(cmd, rc, stdout, stderr)

            if res.returncode == 0 and output_f.exists():
                src_size = file_path.stat().st_size
                out_size = output_f.stat().st_size
                ratio = out_size / src_size if src_size > 0 else 0

                # Integrity gate: 1KB absolute minimum. A valid container
                # header alone is ~1KB; anything below is definitely corrupt.
                # The duration check in _verify_and_finalize (≥95% of source
                # duration) is the real quality gate for high-bitrate sources.
                if out_size > 1024:
                    # Success — resolution/duration/subtitle/finalize happen
                    # in _verify_and_finalize (called by _process_one_file).
                    return True
                else:
                    self.fail_count += 1
                    self.log_msg.emit(
                        f"{self._status_prefix()}FAIL: output too small ({out_size / 1024:.0f} KB)"
                    )
                    # Remove corrupt output
                    output_f.unlink(missing_ok=True)
                    return False
            else:
                stderr_full = res.stderr or ""
                # v4.6.0: scan stdout too. av1an routes chunk-retry
                # noise (encoder stderr dumps, FRAME MISMATCH lines)
                # to stdout, so pattern-matching on stderr alone missed
                # the biggest real-world failure mode (see the FRAME
                # MISMATCH pattern below).
                combined_out = stderr_full + "\n" + (res.stdout or "")
                # v6: Don't increment fail_count yet — we may retry with
                # ffmpeg fallback below. Only increment if the retry also
                # fails (or no retry is possible).
                # v4.4.2: move the FAIL line to _vlog. If the ffmpeg
                # fallback succeeds, the user sees OK. If it also fails,
                # the RETRY FAIL path emits a user-facing FAIL. This way
                # the user doesn't see a confusing "FAIL then OK" for
                # files that av1an choked on but ffmpeg handled.
                self._vlog(
                    f"{self._status_prefix()}av1an failed (exit code {res.returncode}) — attempting ffmpeg fallback"
                )
                self._vlog("  ─── av1an stderr (last 25 lines) ───")
                stderr_lines = stderr_full.splitlines()
                for line in stderr_lines[-25:]:
                    self._vlog(f"  {line}")
                self._vlog("  ────────────────────────────────────")

                # Detect known av1an crash patterns and provide actionable fixes.
                # Pattern table — add new patterns here, no nested ifs below.
                # SEI CERT MSC04-C spirit: single source of truth for diagnostics.
                #
                # v5-03: Added "missing field `streams`" pattern — this is
                # the error av1an emits when its internal ffprobe call
                # returns JSON without a streams field, i.e. the input file
                # is not a valid video. Also added `file` command output
                # to the diagnostic so the user immediately sees "HTML
                # document" (failed yt-dlp download) instead of guessing.
                error_patterns: tuple[tuple[str, str, tuple[str, ...], bool], ...] = (
                    (
                        "Failed to get VSScript API",
                        "av1an cannot initialize VapourSynth — the binary was "
                        "compiled against a different VapourSynth version than "
                        "what is currently installed.",
                        (
                            "  FIX (Arch): yay -S av1an  OR  cargo install av1an --force --locked",
                            "  FIX (Debian): sudo apt install vapoursynth libvapoursynth-script-dev av1an",
                            "  FIX (other): rebuild av1an against current VapourSynth",
                            "  VapourSynth R77+ changed the VSScript API; av1an must be recompiled.",
                        ),
                        True,  # stop queue — every file will hit the same crash
                    ),
                    (
                        "No usable encoder found",
                        "av1an cannot find the encoder binary (SvtAv1EncApp / vpxenc / x265).",
                        (
                            "  Verify the encoder is installed and in PATH.",
                            "  Arch: pacman -S svt-av1 libvpx-tools x265",
                            "  Debian: apt install svt-av1 libvpx-tools x265",
                        ),
                        True,
                    ),
                    # v6-02: av1an scene-detection panic — per-file, not systematic.
                    (
                        "split scores is not empty",
                        "av1an panicked during scene detection (known av1an bug). "
                        "This is a per-file issue — the video content triggered a "
                        "Rust panic in av1an's split module. Will retry with ffmpeg.",
                        (
                            "  This is an av1an internal bug, not a file corruption issue.",
                            "  The file is a valid video — ffmpeg can encode it directly.",
                        ),
                        False,  # don't stop queue — retry with ffmpeg fallback
                    ),
                    (
                        "missing field `streams`",
                        "av1an's internal ffprobe call could not parse this file — "
                        "the file is not a valid video container. This is NOT an "
                        "av1an or ffmpeg bug; the input file itself is invalid.",
                        (
                            "  The file is likely a failed yt-dlp download (HTML error",
                            "  page saved as .mp4), a truncated download, or not a video",
                            "  at all. Run `file <filename>` to confirm.",
                        ),
                        False,  # don't stop queue — other files may be valid
                    ),
                    (
                        "Invalid data found when processing input",
                        "ffmpeg cannot read this input file — the file is corrupt, "
                        "truncated, or not a valid video container.",
                        (
                            "  Run `file <filename>` to see what the file actually is.",
                            "  If it's 'HTML document' or 'ASCII text', it's a failed",
                            "  yt-dlp download — re-download the source video.",
                            "  If it's 'data', the file may be truncated or encrypted.",
                        ),
                        False,
                    ),
                    (
                        "Error: End of file",
                        "av1an hit EOF during scene detection — usually a VapourSynth "
                        "source plugin issue with the intermediate file.",
                        (
                            "  Try a different --chunk-method (override via env probe).",
                            "  If pre-scaling, ensure the intermediate is libx265 CRF 0 (not ffv1).",
                        ),
                        False,
                    ),
                    (
                        "could not open input",
                        "av1an cannot read this input file — possibly corrupt or "
                        "an unsupported codec for the VapourSynth source plugin.",
                        (
                            "  Try playing the file with ffplay to verify it's not corrupt.",
                            "  Run: ffmpeg -i <file> -f null -  to see the decode error.",
                        ),
                        False,
                    ),
                    # v4.0.0: y4m pipe break — Hybrid chunk method can't handle
                    # files with sparse keyframes. This is the "works up until
                    # near the end, never saves chunks into a full file" bug.
                    # The encoder prints a SUMMARY block (it ran briefly on
                    # partial data before the pipe broke), which previously
                    # triggered the v6-03 "concat failure" misdiagnosis. The
                    # retry path switches to --chunk-method select which
                    # extracts frames one-by-one via VapourSynth's select()
                    # filter, avoiding the keyframe-alignment issue.
                    (
                        "Failed to read y4m frame delimiter",
                        "av1an's chunk extractor produced a broken y4m pipe — "
                        "the source's keyframe layout doesn't align with scene "
                        "boundaries. This is the Hybrid chunk method's known "
                        "failure mode for phone-recorded MP4s with sparse "
                        "keyframes (only I-frames every 5-10s). The encoder "
                        "printed a SUMMARY block because it ran briefly on "
                        "partial data before the pipe broke — this is NOT a "
                        "concat failure.",
                        (
                            "  Will retry with --chunk-method select (VapourSynth",
                            "  select() filter), which extracts frames one-by-one",
                            "  and avoids the keyframe-alignment issue.",
                            "  This is per-file, not systematic — subsequent files",
                            "  use select automatically.",
                        ),
                        False,  # don't stop queue — retry with select chunk method
                    ),
                    # v4.6.0: ffmpeg ≥ 7 removed the -vsync option that
                    # av1an's segment/hybrid chunk extraction passes to
                    # ffmpeg. Every segment-based chunk dies immediately
                    # ("Unrecognized option 'vsync'." → empty y4m pipe →
                    # chunk fails 3x). Verified on ffmpeg 9.0.2 + av1an
                    # 0.5.2. select (or a VS source plugin method) is the
                    # only working chunking on these systems.
                    (
                        "Unrecognized option 'vsync'",
                        "av1an's segment/hybrid chunk extraction calls "
                        "`ffmpeg -vsync`, which ffmpeg 7+ removed. Every "
                        "segment-based chunk fails instantly on this system.",
                        (
                            "  FIX: install a VapourSynth source plugin so av1an",
                            "  stops using ffmpeg segmenting: bestsource/ffms2/",
                            "  lsmash (e.g. on Arch: vapoursynth-plugin-bs).",
                            "  Alternatively stay on the default ffmpeg-only path",
                            "  (it doesn't use av1an chunking at all).",
                        ),
                        False,  # per-file — the select override keeps other files working
                    ),
                    # v4.6.0: frame-count drift between av1an's chunk
                    # manifest and what the encoder actually produced.
                    # av1an retries the chunk 3x, then shuts the worker
                    # down with the baffling "encoder crashed: exit
                    # status: 0" (exit 0 because the encode itself
                    # succeeded — on partial data). This was the dominant
                    # large-file failure in the 2026-07-13 av1an log and
                    # previously fell through to "Unknown av1an failure".
                    (
                        "FRAME MISMATCH",
                        "av1an's chunk manifest expected a different frame count "
                        "than the encoder produced — chunk-extraction drift on "
                        "sources with sparse/irregular keyframes. The encode "
                        "itself exits 0 (it ran on partial data), which av1an "
                        "reports as 'encoder crashed: exit status: 0'.",
                        (
                            "  Will retry with --chunk-method select, which",
                            "  extracts exact frame ranges and cannot drift.",
                            "  This is per-file, not systematic — subsequent",
                            "  files use select automatically.",
                        ),
                        False,  # don't stop queue — retry with select chunk method
                    ),
                )
                diagnosis_emitted = False
                for marker, summary, fixes, stop_queue in error_patterns:
                    # v4.6.0: scan stdout + stderr (FRAME MISMATCH and
                    # encoder dumps land in stdout).
                    if marker.lower() in combined_out.lower():
                        # v4.2.1: DIAGNOSIS block is verbose-only. The user
                        # already saw "FAIL: av1an exit code N" above — they
                        # don't need the multi-line root-cause analysis unless
                        # they opt in with --verbose.
                        self._vlog("")
                        self._vlog(f"DIAGNOSIS: {summary}")
                        for fix in fixes:
                            self._vlog(fix)
                        # v5-03: run `file` on the input to tell the user
                        # what the file actually is. This is especially
                        # useful for "missing field streams" and "Invalid
                        # data found" — the user immediately sees "HTML
                        # document" instead of guessing.
                        if marker in ("missing field `streams`",
                                      "Invalid data found when processing input",
                                      "could not open input"):
                            file_type = _identify_file_type(file_path)
                            if file_type:
                                self._vlog(f"  File type: {file_type}")
                                if "HTML" in file_type or "ASCII" in file_type or "text" in file_type:
                                    self._vlog(
                                        "  → This is a TEXT file, not a video. "
                                        "Failed yt-dlp download — re-download the source."
                                    )
                                elif "data" in file_type and "ISO Media" not in file_type:
                                    self._vlog(
                                        "  → File type is 'data' — truncated, encrypted, "
                                        "or partial download."
                                    )
                        if stop_queue:
                            self._stop = True
                            # v4.2.1: STOP reason stays user-facing — the user
                            # needs to know why the queue aborted.
                            self.log_msg.emit(
                                f"  STOP: skipping remaining files ({marker} issue)"
                            )
                        diagnosis_emitted = True
                        break

                if not diagnosis_emitted:
                    # No known pattern matched — show the user where to look.
                    # v6-03: detect "encoder SUMMARY in stderr + non-zero exit"
                    # — the encoder succeeded but av1an failed to produce output.
                    # This is the "chunks but never saves a file" pattern caused
                    # by av1an's concat step failing.
                    # v4.0.0: only treat as concat failure when y4m break is NOT
                    # present. The y4m break pattern (above) emits its own
                    # diagnosis and triggers a retry with --chunk-method select.
                    # The SUMMARY block appears in both cases (encoder ran
                    # briefly before failing), so we must check for the y4m
                    # marker to avoid misdiagnosing chunk-extraction failures
                    # as concat failures.
                    # v4.2.1: all DIAGNOSIS verbose-only.
                    if ("SUMMARY" in stderr_full
                            and "Average Speed" in stderr_full
                            and "Failed to read y4m frame delimiter" not in combined_out
                            and "FRAME MISMATCH" not in combined_out):
                        self._vlog("")
                        self._vlog(
                            "DIAGNOSIS: SVT-AV1 encoder completed successfully (SUMMARY"
                            " block found in stderr), but av1an failed to produce the"
                            " output file. This is an av1an concat failure — the encoder"
                            " did its job but av1an's post-encode merge step crashed."
                        )
                        self._vlog(
                            "  This is a known av1an bug on short videos (1-2 scenes)"
                            " where concat of a single chunk fails. Will retry with"
                            " ffmpeg fallback."
                        )
                    else:
                        self._vlog("")
                        self._vlog(
                            "DIAGNOSIS: Unknown av1an failure. Inspect the full stderr above."
                        )
                        # v5-03: run `file` on the input as a fallback diagnostic.
                        file_type = _identify_file_type(file_path)
                        if file_type:
                            self._vlog(f"  File type: {file_type}")
                        self._vlog(
                            "  Common causes: (1) out of disk space in temp dir, "
                            "(2) AV1 concat failed silently — try installing mkvtoolnix, "
                            "(3) av1an version too old for --concat flag — check av1an --help, "
                            "(4) input file is not a valid video (run `file <filename>`)."
                        )

                # ── v4.0.0: y4m pipe break retry — switch to --chunk-method select ──
                # If av1an failed with the y4m break pattern AND we're not
                # already using select, retry with --chunk-method select. This
                # is faster than the ffmpeg fallback (chunk-parallel still
                # works) and produces identical-quality output (same encoder,
                # same params). Cache the working method so subsequent files
                # skip the wasted first attempt.
                #
                # v4.6.0: FRAME MISMATCH (chunk-extraction drift, see the
                # error-pattern table) joins the y4m break as a drift
                # symptom that select fixes — it was previously an
                # "Unknown av1an failure" that went straight to the slow
                # full-file ffmpeg fallback.
                #
                # NOTE: Do NOT clean up _current_temps before the retry —
                # encode_input (symlink or pre-scaled file) is in
                # _current_temps and the recursive _encode_one call needs it.
                # The finally block below will clean up everything after the
                # recursive call returns (its own finally clears the list
                # first; our finally then runs on an empty list — no-op).
                extraction_drift = (
                    "Failed to read y4m frame delimiter" in combined_out
                    or "FRAME MISMATCH" in combined_out
                    # ffmpeg >= 7 removed -vsync: segment/hybrid chunking
                    # dies instantly while select still works (it uses the
                    # ffmpeg frame server, not segmenting).
                    or "Unrecognized option 'vsync'" in combined_out
                )
                if (not self._stop and extraction_drift
                        and effective_chunk_method != "select"
                        and self.env.av1an_flags.get("has_chunk_method", True)):
                    # v4.2.1: RETRY messages are verbose-only — the user
                    # already saw "FAIL" and will see "SUCCESS" if the retry
                    # works. They don't need to know the retry is happening.
                    self._vlog("")
                    self._vlog(
                        f"  RETRY: Re-encoding {file_path.name} with "
                        f"--chunk-method select (slower but reliable for "
                        f"files with sparse keyframes)..."
                    )
                    output_f.unlink(missing_ok=True)
                    # Cache for subsequent files — avoids the wasted first attempt
                    self.env.av1an_flags["chunk_method_override"] = "select"
                    return self._encode_one(
                        file_path, encode_input, output_f, worker_count,
                        chunk_method="select",
                    )

                # ── v6-01: Per-file av1an→ffmpeg fallback ──
                # If av1an failed for this file AND it's NOT a systematic issue
                # (VSScript API, missing encoder — those set self._stop=True),
                # AND ffmpeg has the encoder for this codec, retry with ffmpeg.
                # This handles:
                #   - av1an concat failures (encoder succeeded but no output)
                #   - av1an scene-detection panics ("split scores is not empty")
                #   - Any other per-file av1an internal failure
                #
                # NOTE: Do NOT clean up _current_temps before the retry —
                # encode_input (symlink or pre-scaled file) is in _current_temps
                # and _ffmpeg_fallback_encode needs it. The finally block below
                # will clean up everything after the retry completes.
                if not self._stop and self._can_ffmpeg_fallback():
                    # v4.2.1: RETRY messages are verbose-only.
                    self._vlog("")
                    self._vlog(
                        f"  RETRY: Attempting ffmpeg fallback for {file_path.name} "
                        f"({self.video_codec.ffmpeg_encoder})..."
                    )
                    # Remove any partial output av1an may have left
                    output_f.unlink(missing_ok=True)
                    # Retry with ffmpeg — _ffmpeg_fallback_encode does NOT
                    # increment fail_count on failure (the caller does that).
                    # If it succeeds, we return True WITHOUT incrementing
                    # fail_count — the file was saved, just via a different path.
                    fb_ok = self._ffmpeg_fallback_encode(
                        file_path, encode_input, output_f,
                    )
                    if fb_ok:
                        self._vlog(
                            f"  RETRY OK: ffmpeg fallback succeeded for {file_path.name}"
                        )
                        return True
                    else:
                        self.fail_count += 1
                        # v4.4.2: this is the ONLY user-facing FAIL for
                        # the av1an path — emitted when both av1an AND
                        # ffmpeg fallback failed. The user sees one line,
                        # not two.
                        self.log_msg.emit(
                            f"{self._status_prefix()}FAIL: av1an + ffmpeg both failed"
                        )
                        self._vlog(
                            f"  RETRY FAIL: ffmpeg fallback also failed for {file_path.name}"
                        )
                        return False
                else:
                    # No retry possible — this is a systematic issue (stop_queue
                    # was set) or ffmpeg lacks the encoder.
                    self.fail_count += 1
                    # v4.4.2: emit user-facing FAIL here too.
                    self.log_msg.emit(
                        f"{self._status_prefix()}FAIL: av1an (no ffmpeg fallback available)"
                    )
                    return False
        except (OSError, subprocess.SubprocessError) as e:
            self.fail_count += 1
            self.log_msg.emit(f"{self._status_prefix()}FAIL: system error: {e}")
            return False
        finally:
            # Always clean this file's temps before moving to next
            self._cleanup_current_temps()

    def _verify_and_finalize(self, file_path, output_f, encode_input, needs_scale):
        """Post-encode verification + subtitle mux + source deletion deferral.

        Runs after a successful _encode_one.  Performs:
          - output resolution verification (if scaling was requested)
          - duration integrity check (>= 95% of source)
          - subtitle mux (if requested)
          - success_count increment + SUCCESS log
          - source deletion deferral (if delete_source is set)

        Returns True if the file was accepted, False if any check failed.
        On failure, fail_count is incremented and output_f is unlinked before
        returning False.  Temp cleanup is the caller's responsibility — it
        differs between the av1an path (already done in _encode_one's finally)
        and the ffmpeg fallback path (done explicitly in _process_one_file).
        """
        # Post-encode resolution verification
        if needs_scale and self.env.ffprobe_path:
            if not _verify_output_resolution(
                output_f, self.env.ffprobe_path,
                self.resolution.width, self.resolution.height,
            ):
                self.fail_count += 1
                self.log_msg.emit(
                    f"{self._status_prefix()}FAIL: resolution verification failed "
                    f"(expected {self.resolution.width}x{self.resolution.height})"
                )
                output_f.unlink(missing_ok=True)
                return False
        src_size = file_path.stat().st_size
        out_size = output_f.stat().st_size
        ratio = out_size / src_size if src_size > 0 else 0

        # Duration integrity check (>= 95% of source)
        dur_ok = True
        dur_info = ""
        if self.env.ffprobe_path:
            src_dur = ffprobe_duration(file_path, self.env.ffprobe_path)
            out_dur = ffprobe_duration(output_f, self.env.ffprobe_path)
            if src_dur and out_dur:
                dur_ratio = out_dur / src_dur
                dur_ok = dur_ratio >= 0.95
                dur_info = f", duration {out_dur:.1f}s/{src_dur:.1f}s ({dur_ratio * 100:.0f}%)"

        if not dur_ok:
            self.fail_count += 1
            self.log_msg.emit(f"{self._status_prefix()}FAIL: duration mismatch{dur_info}")
            output_f.unlink(missing_ok=True)
            return False

        # Mux subtitle if requested (needs source file intact)
        if self.subtitle_lang:
            self._mux_subtitle(file_path, output_f)

        self.success_count += 1
        # v4.2.1: keep user-facing SUCCESS but compact it. Was:
        #   "SUCCESS: filename (1.6MB -> 1.3MB, 81%, duration 15.0s/15.0s (100%))"
        # Now (verbose=False):
        #   "[N/total] filename — OK: 1.6MB -> 1.3MB (81%)"
        # v4.4.0: combined into single line with [N/total] prefix.
        # Verbose mode keeps the duration info on the same line.
        prefix = self._status_prefix()
        if self.verbose:
            self.log_msg.emit(
                f"{prefix}SUCCESS: {src_size / 1_048_576:.1f}MB -> {out_size / 1_048_576:.1f}MB "
                f"({ratio * 100:.0f}%{dur_info})"
            )
        else:
            self.log_msg.emit(
                f"{prefix}OK: {src_size / 1_048_576:.1f}MB -> {out_size / 1_048_576:.1f}MB "
                f"({ratio * 100:.0f}%)"
            )
        # Defer source deletion until after final cleanup
        if self.delete_source:
            self._sources_to_delete.append(file_path)
        return True

    def _cleanup_current_temps(self):
        """Remove all tracked temp files/dirs for the current file.

        Resilient: each removal is try/except'd individually so one bad path
        doesn't block the rest.  Clears the tracking list when done.
        """
        for tf in self._current_temps:
            try:
                if tf.is_dir():
                    shutil.rmtree(str(tf), ignore_errors=True)
                elif tf.exists():
                    tf.unlink()
            except Exception:
                pass
        self._current_temps.clear()

    def _final_cleanup_sweep(self):
        """Residual sweep to catch any orphaned temp files.

        v3 (OTC-013): primary target is now the per-worker subdir
        (``~/.cache/OpenTranscode/tmp/worker-<pid>/``), NOT the shared
        app temp dir. This is safe because the subdir ONLY contains
        this worker's intermediates — a concurrent worker has its own
        subdir. The previous "nuclear" sweep of the entire app temp
        dir was a race-condition risk that this eliminates.
        Also scans in_dir/out_dir as a safety net for legacy temp files
        written by older versions that placed temps next to source files.
        """
        swept = 0

        # v3: sweep ONLY this worker's per-PID subdir, not the shared parent.
        # This is safe — the subdir contains only this worker's intermediates.
        if self._temp_dir.is_dir():
            for hit in self._temp_dir.iterdir():
                try:
                    if hit.is_dir():
                        shutil.rmtree(str(hit), ignore_errors=True)
                    else:
                        hit.unlink(missing_ok=True)
                    swept += 1
                except OSError:
                    # SEI CERT ERR01-C: narrow to OSError (file ops).
                    # Best-effort sweep must not crash on a single bad path.
                    pass

        # Safety-net sweep of user directories (for legacy temp files
        # written by older versions that placed temps next to source files)
        legacy_patterns = ["*.scaled_tmp.mkv", "*.av1an", "*_encodes", "*.*.av1an"]
        for search_dir in (self.in_dir, self.out_dir):
            if not search_dir.is_dir():
                continue
            for pattern in legacy_patterns:
                for hit in search_dir.rglob(pattern):
                    try:
                        if hit.is_dir():
                            shutil.rmtree(str(hit), ignore_errors=True)
                        else:
                            hit.unlink(missing_ok=True)
                        swept += 1
                    except OSError:
                        pass
        # Also clean any orphans still in _current_temps (e.g. stop/crash mid-loop)
        self._cleanup_current_temps()
        # v3: remove the now-empty per-worker subdir itself.
        try:
            self._temp_dir.rmdir()
        except OSError:
            pass  # not empty / not ours — leave it
        if swept:
            self.log_msg.emit(f"CLEANUP: Swept {swept} residual temp file(s)/dir(s).")

    # ── Audio loudness analysis (dual-pass normalization) ──

    def _analyze_audio_loudness(self, file_path: Path) -> float | None:
        """Dual-pass loudnorm analysis for a single file.

        Pass 1: Run loudnorm in analysis-only mode to measure the file's current
        integrated loudness (I) and true peak (TP).

        Returns the dB gain to apply, or None if analysis fails (falls back to
        the knob's static value).
        """
        if not self.env.ffmpeg_path:
            return None
        if abs(self.audio_level_db) < 0.01:
            return None  # knob is at 0 — no normalization requested

        target_lufs = self.audio_level_db  # knob value IS the target LUFS

        try:
            # Pass 1: analyze current loudness
            # v4.6.0: -vn skips video decoding — without it the analysis
            # decoded the ENTIRE video stream just to measure audio
            # loudness, which pushed long/large files past the 120s
            # timeout and silently degraded every big file to the static
            # knob gain.
            analysis_cmd = [
                self.env.ffmpeg_path,
                "-i", str(file_path),
                "-vn",
                "-af", (
                    f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:"
                    f"print_format=json"
                ),
                "-f", "null", "-",
            ]
            res = subprocess.run(
                analysis_cmd, capture_output=True, text=True, timeout=120,
            )

            # Parse the JSON stats from stderr (loudnorm prints to stderr)
            stderr = res.stderr or ""

            # Find the JSON block
            json_match = re.search(r'\{[^{}]*"input_i"[^{}]*\}', stderr, re.DOTALL)
            if not json_match:
                return None

            stats = json.loads(json_match.group())

            input_i = float(stats.get("input_i", "-99"))
            input_tp = float(stats.get("input_tp", "-99"))
            target_tp = float(stats.get("target_tp", "-1.5"))

            # If file is already silent or near-silent, skip
            if input_i <= -70:
                return None

            # Compute the gain loudnorm would apply
            gain_db = target_lufs - input_i

            # Pass 2 concept: check if applying this gain would push peaks
            # above our ceiling. The ceiling is target_tp (default -1.5 dBTP).
            # We want 15% headroom below that ceiling.
            headroom_db = abs(target_tp) * 0.15
            peak_ceiling = target_tp + headroom_db

            # If the file's true peak + gain would exceed the ceiling, clamp
            projected_peak = input_tp + gain_db
            if projected_peak > peak_ceiling:
                gain_db = peak_ceiling - input_tp

            self.log_msg.emit(
                f"  Audio: {input_i:.1f} LUFS -> {target_lufs:.1f} LUFS "
                f"(gain {gain_db:+.1f} dB, peak {input_tp:.1f} -> "
                f"{input_tp + gain_db:.1f} dBTP)"
            )
            return gain_db

        except (OSError, subprocess.SubprocessError, ValueError) as e:
            # ValueError covers json.JSONDecodeError and float() parse failures
            self.log_msg.emit(f"  Audio: loudnorm analysis failed ({e}), using knob value")
            return None

    # ── Subtitle extraction & muxing ──

    def _find_subtitle_stream(self, source: Path, lang: str) -> tuple[int | None, str]:
        """Find subtitle stream in source matching language code.
        Prefers forced disposition tracks. Returns (stream_index, codec_name)."""
        if not self.env.ffprobe_path:
            return (None, "")

        info = ffprobe_validate(source, self.env.ffprobe_path)
        if not info:
            return (None, "")

        forced_match = None
        any_match = None

        for stream in info.get("streams", []):
            if stream.get("codec_type") != "subtitle":
                continue
            tags = stream.get("tags", {})
            if tags.get("language", "").lower() != lang.lower():
                continue

            idx = stream.get("index")
            codec = stream.get("codec_name", "")
            disposition = stream.get("disposition", {})

            if disposition.get("forced") and forced_match is None:
                forced_match = (idx, codec)
            if any_match is None:
                any_match = (idx, codec)

        return forced_match if forced_match else (any_match or (None, ""))

    def _mux_subtitle(self, source: Path, output: Path):
        """Mux a subtitle track from source into the encoded output (soft sub).
        Uses stream copy for MKV; converts to WebVTT for WebM containers."""
        sub_idx, sub_codec = self._find_subtitle_stream(source, self.subtitle_lang)

        if sub_idx is None:
            self.log_msg.emit(f"  SUBS: No {self.subtitle_lang} subtitle found in {source.name}")
            return

        # WebM only supports WebVTT natively; MKV carries any subtitle codec
        is_webm = output.suffix.lower() == ".webm"
        sub_codec_flag = "copy" if not is_webm else "webvtt"

        tmp_out = output.with_suffix(output.suffix + ".submux_tmp")
        try:
            cmd = [
                self.env.ffmpeg_path,
                "-i", str(output),       # encoded output (video + audio)
                "-i", str(source),       # original source (subtitle source)
                "-map", "0",              # all streams from encoded output
                "-map", "-0:s",           # strip any subtitle from output
                "-map", f"1:{sub_idx}",   # subtitle from source
                "-c:v", "copy",
                "-c:a", "copy",
                "-c:s", sub_codec_flag,
                "-y",
                str(tmp_out),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True)

            if res.returncode == 0 and tmp_out.exists() and tmp_out.stat().st_size > 0:
                output.unlink()
                tmp_out.rename(output)
                self.log_msg.emit(
                    f"  SUBS: Muxed {self.subtitle_lang} sub ({sub_codec}) into {output.name}"
                )
            else:
                tmp_out.unlink(missing_ok=True)
                tail = (res.stderr or "")[-200:]
                self.log_msg.emit(f"  SUBS WARN: Remux failed for {output.name}: {tail}")
        except (OSError, subprocess.SubprocessError) as e:
            tmp_out.unlink(missing_ok=True)
            self.log_msg.emit(f"  SUBS ERROR: {e}")

    def stop(self):
        self._stop = True


