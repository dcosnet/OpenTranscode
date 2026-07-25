# Changelog

All notable changes to OpenTranscode. Versions follow semantic versioning.

## [4.5.0] — 2026-07-26 (master)

### Overview
Master release consolidating the v4.4.4 large-file fix with all prior
v4.4.x stability work. Targets the **"every large file fails"** symptom
reported on files from 1.1GB to 20GB, where the lossless pre-scale
intermediate was exhausting the temp partition and presenting as cryptic
`ffmpeg error (rc=234)` messages (the `rc=234` was a truncated 300-char
stderr snippet — the real error was "No space left on device").

### Fixed — "ffmpeg error (rc=234)" on 10GB+ source files with scaling
- **Pre-scale intermediate changed from CRF 0 to CRF 16.** The old CRF-0
  (mathematically lossless) libx265 intermediate produced 2-4× source
  size temp files: a 20GB BluRay rip generated a 60-80GB intermediate,
  exhausted the temp partition, and crashed. CRF 16 is visually lossless
  for archival purposes and produces 0.5-0.8× source size intermediates
  (a 20GB source → ~10-15GB intermediate instead of 60GB). The single
  1.6GB→768MB file that succeeded in the user's batch was the only one
  small enough that the lossless intermediate fit on disk.
- **New `--inline-scale` flag** skips the pre-scale intermediate
  entirely. The scale/pad filter chain is passed directly to av1an via
  `--ffmpeg-filter-args`. Zero intermediate file, one fewer encode pass.
  Toggleable via the new "Inline scale (no intermediate)" checkbox in
  the UI options row. Default OFF — the intermediate path is more
  robust against av1an/VapourSynth filter-arg quirks on older builds.
  Enable when scaling large files (≥10GB) to save disk and time.
- The disk-space pre-check (`_check_disk_space`) now correctly handles
  the inline-scale path: no intermediate is created, so the 2-3× source
  temp-space warning is suppressed.

### Carried forward from v4.4.x
- v4.4.3: `AttributeError: 'EncoderWorker' object has no attribute 'verbose'`
  crash on START in the launcher script. Added UI toggle for av1an
  (checkbox in the options row, equivalent to `--use-av1an`).
- v4.4.2: Live tail of av1an/ffmpeg stderr was spamming the log in
  quiet mode. "FAIL: av1an exit code 1" appeared even when the ffmpeg
  fallback succeeded (confusing "FAIL then OK" double-status).
- v4.4.1: Default log output reduced to two lines per file (start
  banner + finish status). Heartbeat and disk-space warnings require
  `--verbose`.
- v4.4.0: Per-file timeout raised from 2h to 24h (configurable via
  `--timeout`). 5%-of-source integrity check replaced with absolute
  1KB minimum (false-positived on high-bitrate BluRay sources). Disk-
  space pre-check warns (not aborts) when free space < source size.

### Tests added
- `tests/test_inline_scale.py` (13 tests): verifies the CLI flag, the
  `launch_gui` signature, the `EncoderWorker.inline_scale` attribute
  flow, the `_prepare_input` gating, the `_encode_one`
  `--ffmpeg-filter-args` injection, the CRF-16 (not CRF-0) intermediate,
  and that the launcher script mirror stays in sync.

### Files touched in v4.4.4 (carried into 4.5.0)
- `opentranscode/encoder_worker.py` — CRF 16 + `inline_scale` gate +
  `--ffmpeg-filter-args` injection in av1an cmd.
- `open-transcode.py` — mirror of all the above (launcher script).
- `opentranscode/cli.py` — `--inline-scale` flag.
- `opentranscode/ui_window.py` — UI checkbox + `launch_gui` signature +
  state plumbing through `env.av1an_flags["inline_scale"]`.
- `opentranscode/__init__.py` — `launch_gui` wrapper signature updated.

### Upgrade notes
- Default behavior for files **without** a target resolution is
  unchanged — no intermediate is created either way.
- Default behavior for files **with** a target resolution is now
  CRF-16 intermediate (was CRF-0). Output quality is unchanged for
  archival purposes; intermediate size drops ~60-75%.
- For maximum speed on large files with scaling, enable `--inline-scale`
  or check the "Inline scale (no intermediate)" box in the UI. Test on
  a small file first if you're on an older av1an build (pre-0.5.2) to
  confirm `--ffmpeg-filter-args` is accepted.

## [4.4.4] — 2026-07-26

### Fixed — "ffmpeg error (rc=234)" on 10GB+ source files with scaling
- **Pre-scale intermediate changed from CRF 0 to CRF 16.** The old CRF-0
  (mathematically lossless) libx265 intermediate produced 2-4× source
  size temp files: a 20GB BluRay rip generated a 60-80GB intermediate,
  exhausted the temp partition, and crashed with cryptic
  `ffmpeg error (rc=234)` messages (the rc=234 came from a truncated
  300-char stderr snippet — the real error was "No space left on
  device"). CRF 16 is visually lossless for archival purposes and
  produces 0.5-0.8× source size intermediates (a 20GB source →
  ~10-15GB intermediate instead of 60GB).
- **New `--inline-scale` flag** skips the pre-scale intermediate
  entirely. The scale/pad filter chain is passed directly to av1an via
  `--ffmpeg-filter-args`. Zero intermediate file, one fewer encode pass.
  Toggleable via the new "Inline scale (no intermediate)" checkbox in
  the UI options row. Default OFF — the intermediate path is more
  robust against av1an/VapourSynth filter-arg quirks on older builds.
  Enable when scaling large files (≥10GB) to save disk and time.
- The disk-space pre-check (`_check_disk_space`) now correctly handles
  the inline-scale path: no intermediate is created, so the 2-3× source
  temp-space warning is suppressed.

### Tests added
- `tests/test_inline_scale.py` (13 tests): verifies the CLI flag, the
  `launch_gui` signature, the `EncoderWorker.inline_scale` attribute
  flow, the `_prepare_input` gating, the `_encode_one`
  `--ffmpeg-filter-args` injection, the CRF-16 (not CRF-0) intermediate,
  and that the launcher script mirror stays in sync.

## [4.4.3] — 2026-07-25

### Fixed
- `AttributeError: 'EncoderWorker' object has no attribute 'verbose'` crash
  on START in the launcher script's `EncoderWorker.__init__`. The launcher
  script now sets `self.verbose` from `env.av1an_flags["verbose"]`, matching
  the package's behavior.

### Added
- UI toggle for av1an: a new "av1an (chunk-parallel)" checkbox in the
  options row. Default OFF = ffmpeg-only. The CLI flag `--use-av1an`
  still works; the UI toggle takes precedence when set.

### Changed
- Documentation terminology: `open-transcode.py` is consistently called
  "the launcher script" (not "single-file script"). It mirrors the
  16-module `opentranscode/` package; calling it "single-file" was
  misleading.

## [4.4.2] — 2026-07-25

### Fixed
- Live tail of av1an/ffmpeg stderr (`│ Encoding: 1373/1376 Frames @ 51.70 fps...`)
  was spamming the log in quiet mode in the launcher script. The package
  had this gated behind `--verbose` since v4.1.1; the launcher script
  now matches.
- "FAIL: av1an exit code 1" appeared in the log even when the ffmpeg
  fallback succeeded, producing a confusing "FAIL then OK" double-status.
  The av1an failure line now goes to `_vlog` (verbose only); the user
  sees only the final outcome (OK or `FAIL: av1an + ffmpeg both failed`).

## [4.4.1] — 2026-07-25

### Changed
- Default log output reduced to two lines per file: start banner + finish
  status. Heartbeat (`... 30s elapsed`) and disk-space warnings now
  require `--verbose`. The user asked for "start + finish, nothing else";
  this delivers exactly that.

## [4.4.0] — 2026-07-25

### Added — massive-file support (30GB+ BluRay rips)
- **Per-file timeout raised from 2h to 24h**, configurable via
  `--timeout SECONDS`. A 30GB 1080p BluRay rip at SVT-AV1 preset 6
  takes 4-10 hours; the old 2h timeout killed massive-file encodes
  partway through.
- **5%-of-source integrity check replaced with absolute 1KB minimum**.
  The old check false-positived on high-bitrate sources (50GB BluRay →
  5% = 2.5GB, but valid AV1 at CRF 32 produces 1-2GB for a 2-hour movie).
  The real integrity gate is the duration check (≥95% of source).
- **Disk-space pre-check** warns (not aborts) if free space < source size.
  When scaling, also checks the temp partition (lossless intermediate
  can be 2-3x source size).

### Changed — log noise reduction
- Combined `[N/total] filename` banner + status into a single line:
  `[1/180] filename.mkv — OK: 1.6MB -> 1.3MB (81%)` (was two lines).
- Disk-space warnings no longer fire for skipped files (the check now
  runs after the skip-existing check).

## [4.3.0] — 2026-07-25

### Added — skip-existing detection
- Probes the output file with ffprobe before encoding. If the output
  exists with a matching video+audio codec (and matching resolution when
  scaling is requested), the file is skipped. Default ON; use
  `--force-reencode` to disable.
- Added `ffprobe_codec_name` field to `VideoCodecProfile` and
  `AudioProfile` (av1/vp9/hevc, opus/vorbis/flac/iamf).
- Final summary now includes `Skipped: N` count.

### Fixed — heartbeat regression
- v4.2.1 gated the 30-second heartbeat behind `--verbose`, causing the
  "hangs on first transcode, forever timer" symptom in quiet mode. The
  heartbeat is now always user-facing (one line per 30 seconds during
  long encodes). The live tail of `frame= 67 fps= 12...` stays gated.

## [4.2.1] — 2026-07-25

### Changed — quiet mode by default
- Tech-detail log lines gated behind `--verbose`. Default output is
  two lines per file: start banner + finish status.
- Gated: CMD: lines, live tail of av1an/ffmpeg stderr, DIAGNOSIS blocks,
  resolution map, pre-flight validation table, heartbeat, disk-space
  warnings, RETRY messages, file-type detection details.

## [4.2.0] — 2026-07-25

### Changed — ffmpeg is the default encode path
- av1an chunk-parallel was too fragile across distros (y4m pipe breaks,
  SvtAv1EncApp CLI rejects `--threads`, VapourSynth plugin issues,
  output buffering making it look hung). The default encode path is now
  ffmpeg-only. av1an is opt-in via `--use-av1an`.
- The av1an pre-flight smoke test is skipped entirely when av1an is
  not requested. `_on_run_clicked` sets `use_ffmpeg_fallback = True`
  directly, short-circuiting the av1an code path.

## [4.1.2] — 2026-07-25

### Fixed
- Removed the `--threads N` injection into av1an's `--video-params`
  string (introduced in v4.1.0). `SvtAv1EncApp` (the standalone CLI
  av1an invokes per-chunk) does not accept `--threads` — only `--lp`
  (logical processors). The result was `Unprocessed tokens: --threads`
  → every chunk failed 3x → no av1an output. Thread capping now lives
  in av1an's `--workers` flag (chunk-parallel count) and in `-threads`
  for the ffmpeg fallback path (where libsvtav1 is a library).
- `params_fn` signature returned to `(crf, preset) -> str` (v4.0.0 form).

## [4.1.1] — 2026-07-25

### Added
- **Live progress tail** — av1an's stdout/stderr emits to the GUI log
  as it arrives (handles both `\n` log lines and `\r` progress bar
  updates as line boundaries).
- **30-second heartbeat** — `... still encoding (Xs elapsed)` every
  30 seconds so the user knows the encode is alive.

### Changed
- `IDEAL_THREADS_PER_WORKER` raised from 4 to 6 for better per-chunk
  SVT-AV1 throughput. On a 28-thread Xeon, the split changed from
  6×4=24 to 4×6=24 (same total, better per-chunk latency).

## [4.1.0] — 2026-07-25

### Added — intelligent chunking
- `_compute_intelligent_worker_count()` computes `(worker_count,
  threads_per_worker)` such that `worker_count * threads_per_worker <=
  logical_threads - 1`. Prevents thread oversubscription on high-core-
  count machines (13 workers × 28 threads = 364 active on 28 logical CPUs
  → kernel scheduler drowned → hard lock).
- Per-encoder `--threads N` cap injected into `--video-params`.
- CLI flags `--max-workers N` and `--threads-per-worker N` for overrides.

## [4.0.0] — 2026-07-25

### Fixed — "works up until near the end, never saves chunks into a full file"
- av1an auto-selects the Hybrid chunk method when no VapourSynth source
  plugins are installed. Hybrid fails on phone-recorded MP4s with sparse
  keyframes (scene boundaries rarely align with I-frames → segment muxer
  splits mid-GOP → decoder errors → y4m pipe breaks → encoder reads EOF
  → every chunk fails after 3 retries → no output file).
- `env_probe` now probes for VapourSynth source plugins (`lsmash`,
  `ffms2`, `bestsource`, `dgdecnv`). When none are found, pre-sets
  `chunk_method_override = "select"` to avoid the wasted first attempt.
- `_encode_one` accepts a `chunk_method` parameter for retry. When av1an
  fails with the y4m break pattern, it recursively retries with
  `--chunk-method select` and caches that choice for subsequent files.
- `--chunk-method {auto,select,hybrid,segment,ffms2,lsmash,bestsource,dgdecnv}`
  CLI flag for forcing a specific chunk method.

### Package split
- Refactored the monolithic `open-transcode.py` into a 16-module
  `opentranscode/` package. The launcher script is preserved for
  backwards compatibility and as the test target for mocked tests.
- `pyproject.toml` for `pip install -e .` and `python -m build`.

---

[4.5.0]: https://git.dcos.net/dcosnet/OpenTranscode/releases/tag/v4.5.0
[4.4.4]: https://git.dcos.net/dcosnet/OpenTranscode/releases/tag/v4.4.4
[4.4.3]: https://git.dcos.net/dcosnet/OpenTranscode/releases/tag/v4.4.3
[4.4.2]: https://git.dcos.net/dcosnet/OpenTranscode/releases/tag/v4.4.2
[4.4.1]: https://git.dcos.net/dcosnet/OpenTranscode/releases/tag/v4.4.1
[4.4.0]: https://git.dcos.net/dcosnet/OpenTranscode/releases/tag/v4.4.0
[4.3.0]: https://git.dcos.net/dcosnet/OpenTranscode/releases/tag/v4.3.0
[4.2.1]: https://git.dcos.net/dcosnet/OpenTranscode/releases/tag/v4.2.1
[4.2.0]: https://git.dcos.net/dcosnet/OpenTranscode/releases/tag/v4.2.0
[4.1.2]: https://git.dcos.net/dcosnet/OpenTranscode/releases/tag/v4.1.2
[4.1.1]: https://git.dcos.net/dcosnet/OpenTranscode/releases/tag/v4.1.1
[4.1.0]: https://git.dcos.net/dcosnet/OpenTranscode/releases/tag/v4.1.0
[4.0.0]: https://git.dcos.net/dcosnet/OpenTranscode/releases/tag/v4.0.0
