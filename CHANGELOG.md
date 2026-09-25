# Changelog

All notable changes to OpenTranscode. Versions follow semantic versioning.

## [4.8.1] — 2026-09-25

### Changed — GPU dropdown / ENGINE interaction
- **ENGINE = CPU now disables the GPU dropdown** (greyed out with an
  explanatory tooltip) — the choice would have no effect there.
- **"None (CPU-only encode)" remains an explicit option** in the GPU
  dropdown for the opposite case: a GPU present in the box that the
  user doesn't want encoding on (e.g. it's the display card). The two
  controls can no longer contradict each other: GPU engines + "None"
  resolve to the CPU path by design, and CPU engine ignores the GPU
  choice entirely.

## [4.8.0] — 2026-09-25

### Added — GPU capability profiles (combined generations, incl. oddballs)
- **New `gpu_profiles.py` + GPU dropdown** (UI, next to ENGINE; CLI
  `--gpu-profile`). Entries combine whole card generations into single
  capability classes — same silicon, same encoding behaviour:
  - NVIDIA Kepler/Maxwell (H.264 only), Pascal (GTX 10-series + Tesla
    P40/P4/P100 — H.264+HEVC 8/10-bit), Turing (RTX 20 / GTX 16 /
    Tesla T4 / **CMP 30/40/50HX** — +B-frames), Ampere (RTX 30 /
    A10/A40 / **CMP 90HX** — no AV1 encode), Ada/Blackwell (RTX 40/50,
    L4/L40 — +AV1 10-bit).
  - Oddballs: **NVIDIA data-center compute (V100/A100/H100, CMP
    170HX) has NO NVENC silicon** — the profile routes to CPU instead
    of failing. CMP 170HX is GA100-based: the fastest mining card that
    cannot hardware-encode.
  - Intel Arc (QSV: H.264+HEVC+AV1) and Iris/UHD; AMD RDNA 3 (VAAPI:
    +AV1 encode), RDNA 1/2, and the crypto-era GCN 4/5 + Vega cards.
- **Auto-detection**: the probe matches the detected GPU name
  (nvidia-smi / lspci, vendor-aware) to a profile; the live encode
  smoke test decides what actually works. VAAPI encodes via
  `-vaapi_device` + `hwupload`, QSV via `-init_hw_device qsv=hw`.
- **Profile-driven encoding**: `resolve_gpu_encoder()` now returns
  (encoder, api) and the ffmpeg command is built per API (device init
  args, hwupload filter chains, per-API quality args — nvenc
  `-rc vbr -cq`, QSV `-global_quality`, VAAPI `-rc_mode CQP`).
- **Rebuild-from-git builds for the selected GPU profile**: the dep
  tree extends with the vendor's packages (arch: nv-codec-headers /
  libva+libdrm+mesa / intel-media-driver+onevpl; debian/redhat/suse
  equivalents), so pressing REBUILD on a bare system generates the GPU
  dependency tree too.
- Forced profiles let a user pin a capability class even when name
  auto-match fails; "Auto-detect" stays the default.

## [4.7.1] — 2026-09-25

### Added — always-usable REBUILD FROM GIT (self-generating dep tree)
- **The REBUILD FROM GIT button is now ALWAYS enabled.** It no longer
  depends on a successful environment probe — a failed probe (missing
  binaries, broken VSScript, missing av1an) is exactly when the rebuild
  is needed, so the button works from first launch on a bare system.
- **The build generates its own dependency tree.** Instead of the old
  pacman-only toolchain list, `build_dep_plan()` maps the detected distro
  family to the right package set and installer: arch (pacman), debian
  (apt-get), redhat (dnf), suse (zypper) — including the previously
  missing **zimg** (VapourSynth's one hard library dependency, which
  made the meson step fail on bare systems), meson/ninja/cmake/nasm and
  rust for av1an. Unsupported families get an explicit manual-install
  note. Critical tools are re-verified after install; the build aborts
  with the exact list if anything is still missing.
- **BestSource plugin now builds from git as part of the VapourSynth
  rebuild** (cloned with its libp2p submodule, compiled against the
  freshly installed git-VS headers via PYTHONPATH/PKG_CONFIG_PATH, into
  the user site-packages plugins dir). This closes the av1an chunking
  gap: with BestSource present, av1an auto-selects the fast chunk
  method instead of quadratic-decode `select`. The full chain was
  verified live on this machine: VS git (Core R80) → BestSource →
  `av1an --chunk-method bestsource` → rc=0 output.
- **Runtime env follows the git stack**: `_av1an_env()` and the plugin
  probe now include the python user-site vapoursynth dir (module + libs
  + plugins), so av1an loads the freshly built VS instead of the system
  one after a rebuild.

## [4.7.0] — 2026-09-25

### Added — Hybrid GPU + CPU scheduling
- **New engine: Hybrid (UI ENGINE combo / `--engine hybrid`).** The
  queue is scanned once, then split between two CONCURRENT lanes: a GPU
  lane (NVENC via single-pass ffmpeg) and a CPU lane (the family's
  software encoder, or av1an chunk-parallel when opted in — so NVENC +
  chunk workers + software encoders can all run at the same time on
  multi-core boxes with an NVIDIA card).
- **LPT load balancing** (`hybrid_scheduler.plan_hybrid`): files sorted
  by size descending, each assigned to the lane with the lower
  estimated load using a GPU:CPU speed ratio (default 8:1) — both lanes
  finish at roughly the same time.
- **CPU lane thread budget**: the CPU lane's topology is reduced by a
  2-thread reserve for the GPU lane's decode/scale/mux before the
  intelligent worker math runs; the lane's software ffmpeg encodes are
  additionally capped with `-threads N`. NVENC jobs are never
  thread-capped (silicon-bound).
- **Per-lane temp dirs** (`worker-<pid>-gpu` / `worker-<pid>-cpu`):
  both lanes share one process, so the PID alone no longer separates
  them — a lane finishing early can no longer sweep the other lane's
  intermediates.
- STOP stops both lanes; the final summary aggregates both lanes'
  results. Hybrid needs 2+ encodable files and a functional GPU encoder
  and otherwise falls back to a single CPU queue with a logged reason.
  Lanes split FILES, never one file across encoders (mixed-encoder
  chunks would produce visibly inconsistent quality within a file, and
  av1an cannot drive NVENC).

### Fixed — during hybrid hardening
- **libx265 rejects large `-threads` values** ("frameNumThreads must be
  [0 .. X265_MAX_FRAME_THREADS)"): the CPU lane's injected thread cap is
  clamped to 16 for libx265; SVT-AV1 and libvpx keep the full budget.
- **Failed ffmpeg encodes now delete their partial output.** A failed
  encode used to leave a truncated file that ffprobe still parses as the
  right codec/resolution — skip-existing would then treat it as a
  finished archive forever. The output is unlinked on the ffmpeg error
  path (the av1an and STOP paths already cleaned up).

### Fixed — launcher crash on STARTUP
- `OpenCodecMaster._build_ui()` read `self.env.av1an_flags` while
  pre-selecting the ENGINE combo, but `env` is None/absent until
  `_probe_and_init` runs after the UI build — instantiating the window
  crashed with `AttributeError: ... has no attribute 'env'` (launcher)
  or on `NoneType` (package). The pre-select now reads defensively.
  Verified by offscreen-instantiating the real launcher window.

### Tests
- `tests/test_hybrid_scheduler.py` (19): LPT split, degenerate cases,
  thread budget, `file_subset` end-to-end, per-lane temp dirs, ffmpeg
  thread cap (CPU yes / GPU no), `scan_input_files`, CLI `--engine
  hybrid`, launcher parity.

## [4.6.0] — 2026-09-25

### Overview
GPU (NVENC) encoding with auto GPU/CPU engine selection, the root-cause
fix for the av1an chunking failures on ffmpeg 7+, and the remaining
large-file failure modes (pre-scale timeout, silent disk exhaustion,
whole-video loudnorm decode).

### Added — GPU (NVENC) support + auto engine
- **Engine selector** (UI: Auto/GPU/CPU combo; CLI: `--engine
  {auto,gpu,cpu}`). Auto uses the NVENC hardware encoder for the
  selected codec family (x265 → `hevc_nvenc`, AV1 → `av1_nvenc` on
  RTX 40+) when it actually works; VP9 has no NVENC encoder and stays
  on CPU. GPU encodes run via single-pass ffmpeg with `-rc vbr -cq N`
  (CPU decode), so av1an chunk-parallel is not needed — one NVENC
  process outruns chunk-parallel CPU workers.
- **`env_probe.GpuInfo` + `_probe_gpu()`**: two-stage NVENC probe —
  compiled-in encoder list, then a LIVE encode smoke test per encoder.
  The live gate catches the real-world failure mode where ffmpeg lists
  `hevc_nvenc` but the installed NVIDIA driver is older than the NVENC
  API the build targets ("Driver does not support the required nvenc
  API version. Required: 13.1 Found: 13.0" — observed on GTX 1070 +
  driver 580 + ffmpeg 9.0.2), reports the driver fix, and the engine
  falls back to CPU automatically.
- Skip-existing is engine-agnostic: `hevc_nvenc` outputs the same
  ffprobe codec_name (`hevc`) as libx265, so switching engines never
  re-encodes finished files.
- `--dry-run` prints the GPU verdict (usable encoders or the exact
  failure detail).
- **`scripts/build-ffmpeg-nvenc-matched.sh`** — fixes the
  driver/ffmpeg NVENC API mismatch without touching the system:
  builds ffmpeg against the nv-codec-headers gen the installed driver
  actually provides (e.g. 580 driver → gen 13.0) with the distro
  build's exact feature set, installing to `~/.local` with
  `--enable-rpath` (critical — without rpath the binary silently
  loads the distro libavcodec and keeps demanding the newer API).
  Verified on GTX 1070 + driver 580.178 + ffmpeg 9.0.2: hevc_nvenc
  and h264_nvenc functional, av1_nvenc correctly reported as
  unavailable (no Pascal AV1 hardware) and auto stays on SVT-AV1.

### Fixed — av1an chunking (root cause on ffmpeg 7+)
- **ffmpeg ≥ 7 removed `-vsync`, which av1an's segment/hybrid chunk
  extraction passes to ffmpeg.** Every segment-based chunk died
  instantly ("Unrecognized option 'vsync'" → broken y4m pipe → chunk
  fails 3×) — this produced the y4m pipe-break storm in the 2026-07-13
  av1an log. Verified end-to-end on ffmpeg 9.0.2 + av1an 0.5.2:
  `select` still works (it uses the ffmpeg frame server, not
  segmenting), so the plugin-less `select` override remains, the
  failure is now diagnosed with an actionable block (install
  bestsource/ffms2/lsmash, or stay on the ffmpeg-only path), and the
  select retry now also triggers on it.
- **FRAME MISMATCH ("encoder crashed: exit status: 0")** — chunk
  manifest vs encoded frame-count drift on sparse-keyframe sources
  (20 worker shutdowns in the same log) fell through to "Unknown av1an
  failure". Now a recognized per-file pattern that retries with
  `select` (exact frame ranges can't drift). Diagnostics now scan
  av1an's stdout too, since FRAME MISMATCH lines land there.

### Fixed — large-file failures
- **The ffmpeg path no longer pre-scales.** The CRF-16 intermediate
  existed only for VapourSynth source-plugin compatibility; the
  ffmpeg/GPU path scales inline via `-vf`. This removes the whole
  class of failures: the 0.5-0.8× source-size temp file, the extra
  full encode pass, and the flat **1800s pre-scale timeout** that
  killed long/high-bitrate sources at exactly 30 minutes. The av1an
  path keeps the intermediate but now runs under the per-file timeout
  with STOP-button support ("FAIL: pre-scale timeout" on expiry).
- **Severe disk-space warnings are user-facing.** "free < source size"
  on the output or temp partition was verbose-only — quiet mode gave
  zero notice before "No space left on device". Marginal advice stays
  verbose-only.
- **Loudnorm analysis is audio-only (`-vn`).** It previously decoded
  the entire video stream to measure audio loudness, pushing large
  files past the 120s analysis timeout and silently degrading them to
  the static knob gain.

### Tests
- `tests/test_gpu_engine.py` (32): GpuInfo, the live-encode GPU gate
  (driver-mismatch case included), the resolve matrix, NVENC vargs,
  CLI `--engine`, worker engine plumbing, launcher parity.
- `tests/test_large_file_fixes.py` (11): pre-scale gating + timeout +
  STOP handling, user-facing disk-space warnings, `-vn` loudnorm,
  FRAME MISMATCH / vsync select retries.
- Harness: EncoderWorker has class-level defaults for `verbose` /
  `_current_*` so `__new__`-built test instances match the v4.4.3
  contract; tests replace the Qt signal with `conftest.capture_signal()`
  instead of patching read-only `SignalInstance` attributes (fixes all
  24 order-dependent failures on machines with a real PySide6).

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
