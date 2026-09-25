# OpenTranscode

**Open-source batch video transcoder for Linux.** Encodes folders of video
files to AV1 / VP9 / HEVC with configurable audio codecs, resolution
scaling, and source-file management. Built on ffmpeg (default) with an
optional av1an chunk-parallel path for users with a working VapourSynth
setup. NVIDIA GPU (NVENC) encoding is used automatically when available.

- **Default encoder**: ffmpeg + libsvtav1 (reliable across distros)
- **GPU encoder**: NVENC (hevc_nvenc / av1_nvenc) auto-detected with a
  live encode test — used by the Auto engine when it actually works
- **Optional encoder**: av1an chunk-parallel (opt-in via UI toggle)
- **Self-sufficient rebuild**: the always-enabled REBUILD FROM GIT
  button installs its own build dependencies (distro-aware), then builds
  VapourSynth + BestSource + av1an into `~/.local` / `~/.cargo`
- **Codecs**: AV1 (SVT-AV1), VP9, x265 (HEVC) video; Opus, Vorbis, FLAC, IAMF audio
- **Containers**: MKV, WebM, MP4
- **Resolution**: Original or scaled (16:9, 21:9, 32:9 presets from 480p to 4K)
- **Skip-existing**: Probes output with ffprobe; skips files whose codec matches
- **Audio normalization**: Per-file loudness analysis with volume gain
- **Subtitle mux**: Optional English subtitle passthrough
- **Source management**: Optional verified-source deletion after encode

## Requirements

- Linux (POSIX)
- Python ≥ 3.12
- ffmpeg (with libsvtav1, libvpx, libx265, libopus, libvorbis, flac)
- ffprobe
- PySide6 (for the GUI)
- Optional: NVIDIA GPU + driver for NVENC hardware encoding
- Optional: av1an + VapourSynth (only if using the av1an toggle)

## Quick start

### Install as a package (recommended)

```bash
cd /path/to/opentranscode
pip install -e .

opentranscode                        # launch the GUI
python -m opentranscode --version    # → opentranscode 4.6.0
python -m opentranscode --help
```

### Run the launcher script (backwards compat)

```bash
python open-transcode.py             # launches the GUI
```

### Verify environment without encoding

```bash
opentranscode --dry-run              # probe + smoke test, no encode
opentranscode --verify-only FILE.mkv # re-verify an existing output
```

## Usage

### Encode engine (GPU vs CPU)

The ENGINE selector (UI) or `--engine` flag picks the video encoder:

- **Auto (default)** — uses the NVENC hardware encoder for the selected
  codec family when the environment probe's *live encode test* proved it
  works (x265 → `hevc_nvenc`, AV1 → `av1_nvenc` on RTX 40+); otherwise
  the software encoders. VP9 has no NVENC encoder and always uses the CPU.
- **GPU** — force NVENC; falls back to CPU with a log line when
  unavailable. GPU encodes run via single-pass ffmpeg (`-c:v hevc_nvenc
  -rc vbr -cq N`), with CPU decode. av1an chunk-parallel is not used —
  one NVENC process outruns chunk-parallel CPU workers.
- **CPU** — force the software encoders (SVT-AV1 / VP9 / x265).
- **Hybrid** (v4.7.0) — run the GPU lane AND a CPU lane **concurrently**:
  the queue is scanned up front and split by file size (LPT, balanced
  with a GPU:CPU speed ratio) so both lanes finish at about the same
  time. The CPU lane's thread budget is reduced by a small reserve for
  the GPU lane's decode/mux, and with `--use-av1an` the CPU lane uses
  chunk-parallel — so NVENC + chunk workers + software encoders can all
  be busy at once. Lanes split FILES, never one file across encoders
  (mixing hevc_nvenc and libx265 chunks inside a single file would make
  scene-by-scene quality visibly inconsistent, and av1an cannot drive
  NVENC anyway). Needs 2+ encodable files and a functional GPU encoder;
  otherwise it falls back to a single CPU queue. GPU-lane files get
  NVENC quality, CPU-lane files get software-encoder quality.

The GPU gate is a real encode smoke test, not just an encoder-list grep:
a ffmpeg build can list `hevc_nvenc` while the installed NVIDIA driver is
too old for the NVENC API version it was compiled against (e.g. "Driver
does not support the required nvenc API version. Required: 13.1 Found:
13.0") — in that case the probe reports the exact driver fix and encoding
stays on the CPU. `opentranscode --dry-run` prints the GPU verdict.

#### When NVENC is "present but NOT usable" (driver / ffmpeg API mismatch)

ffmpeg is compiled against a specific NVENC API version and the NVIDIA
driver must be at least that new. On legacy driver branches (580 is the
**last** branch supporting Pascal cards such as the GTX 10xx) upgrading
the driver is not an option — so invert the fix: build ffmpeg against the
NVENC API your driver *does* provide and install it to `~/.local/bin`
(which PATHs before the distro binary; OpenTranscode picks it up
automatically on the next launch). Use the included helper:

```bash
# "Found: 13.0" in the error → pass 13.0
./scripts/build-ffmpeg-nvenc-matched.sh 13.0
```

The script copies your distro ffmpeg's feature set (same version, same
libraries — a drop-in), builds against the matching `nv-codec-headers`,
and installs only into `$HOME/.local`. Nothing system-wide is touched.

### Default (ffmpeg-only, recommended)

```bash
opentranscode
```

- Encodes with `ffmpeg -c:v libsvtav1` (or libvpx-vp9 / libx265 based on selection)
- Single-pass per file
- Reliable across distros; no VapourSynth dependency

### Skip-existing (default ON)

Files whose output already exists with a matching video+audio codec are
skipped. Detection uses ffprobe — verifies `codec_name` for both video
and audio streams, plus resolution when scaling is requested.

```bash
opentranscode                       # skip-existing ON (default)
opentranscode --force-reencode      # re-encode everything
```

### Verbose logging

Default log output is minimal — two lines per file (start + finish):

```
Found 180 file(s) to process.
[1/180] filename.mkv
[1/180] filename.mkv — OK: 1.6MB -> 1.3MB (81%)
[2/180] already_done.mkv — SKIP (already av1/opus)
[3/180] next.mkv
[3/180] next.mkv — OK: 2.4MB -> 1.8MB (75%)
QUEUE COMPLETE. Success: 178, Failed: 0, Skipped: 2.
```

For diagnostics (CMD lines, live tail of ffmpeg/av1an stderr, disk-space
warnings, heartbeats):

```bash
opentranscode --verbose
```

### Massive-file support

For 30GB+ BluRay rips:

- **24-hour per-file timeout** (configurable via `--timeout SECONDS`)
- **Inline scaling on the ffmpeg path** — the ffmpeg (and GPU) path scales
  with `-vf` directly in the encode command; no intermediate file, no
  extra encode pass, no pre-scale timeout. The av1an path still writes a
  CRF-16 (visually lossless) intermediate for VapourSynth compatibility,
  but under the full per-file timeout instead of a flat 30-minute cap.
- **1KB absolute integrity minimum** (no false "output too small" failures
  on high-bitrate sources — duration check is the real gate)
- **Disk-space warnings** — severe warnings (free space below the source
  size on the output or temp partition) are user-facing even in quiet mode

```bash
opentranscode --timeout 36000       # 10h per-file timeout
```

### GPU capability profiles

The GPU dropdown (next to ENGINE) lists combined card generations —
same silicon, same encoding behaviour — rather than individual SKUs:
NVIDIA Kepler through Ada/Blackwell, Intel Arc + integrated (QSV), and
AMD GCN/RDNA (VAAPI), including data-center and crypto-era oddballs
(Tesla P40/T4, CMP 30–90HX, and the GA100-based CMP 170HX + V100/A100/
H100 boards, which ship without NVENC and route to the CPU path).
Auto-detect matches your card and the live encode probe decides what
works; forcing a profile extends the REBUILD FROM GIT dependency tree
with that GPU's packages.

### Rebuilding the encode stack from git

The REBUILD FROM GIT button is available at all times — even on a bare
system with no build tools installed:

1. **Dependency tree** — detects missing tools/libraries and installs
   them via the distro package manager (pacman/apt-get/dnf/zypper; one
   privilege prompt). Includes zimg, which VapourSynth requires.
2. **VapourSynth from git** → `~/.local` (self-contained: module, libs,
   headers in the python user site-packages).
3. **BestSource plugin from git** → compiled against that fresh VS, so
   av1an gets a fast, frame-accurate chunk method (no more slow
   `select`).
4. **av1an from git** → `~/.cargo/bin`.

Restart the app afterwards; the probe picks up the new stack
automatically. `ffmpeg + IAMF` builds libiamf + ffmpeg into
`~/.local/bin` for the IAMF audio codec.

### av1an chunk-parallel (opt-in)

For users with a working VapourSynth + source plugin (lsmash, ffms2,
bestsource) setup who want scene-detection-based chunk-parallel encoding:

- **UI**: Check the "av1an (chunk-parallel)" checkbox
- **CLI**: `opentranscode --use-av1an`

Known chunking failure modes (all handled with per-file retries + a
diagnosis block, and safe to hit):

- **ffmpeg ≥ 7 removed `-vsync`** — av1an's segment/hybrid chunk
  extraction calls `ffmpeg -vsync` and dies with "Unrecognized option
  'vsync'" on modern ffmpeg. Without VS source plugins the app forces
  `--chunk-method select` (works on any ffmpeg — verified — but is slow,
  so the default ffmpeg-only path is usually faster for long files).
  Install `bestsource`/`ffms2`/`lsmash` to give av1an a fast plugin
  chunk method.
- **FRAME MISMATCH / "encoder crashed: exit status: 0"** — chunk
  extraction drift on sources with sparse keyframes; the app retries the
  file with `select` automatically.
- **y4m pipe breaks** ("Failed to read y4m frame delimiter") — same
  select retry.

When av1an fails per-file, the code automatically falls back to ffmpeg
for that file. When av1an fails systematically (VSScript API mismatch,
missing encoder), the queue aborts with an actionable diagnostic.

## CLI reference

```
opentranscode [--version] [--dry-run] [--verify-only PATH] [--force]
              [--engine {auto,gpu,cpu,hybrid}]
              [--chunk-method METHOD] [--max-workers N] [--threads-per-worker N]
              [--use-av1an] [--verbose] [--skip-existing | --force-reencode]
              [--timeout SECONDS]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--version` | — | Print version and exit |
| `--dry-run` | — | Probe environment + smoke test, no encode |
| `--verify-only PATH` | — | Re-verify an existing output file |
| `--force` | off | Skip ffprobe pre-validation |
| `--engine` | auto | `auto` = NVENC GPU encoder when the live encode test passes, else CPU; `gpu` = force NVENC; `cpu` = force software encoders |
| `--chunk-method METHOD` | auto | Force av1an chunk method (select, hybrid, ffms2, lsmash, bestsource, dgdecnv) |
| `--max-workers N` | auto | Cap chunk-parallel worker count |
| `--threads-per-worker N` | auto | Per-encoder thread cap |
| `--use-av1an` | off | Use av1an chunk-parallel (UI toggle also available) |
| `--verbose` | off | Full tech-detail log output |
| `--skip-existing` | on | Skip files whose output has matching codec |
| `--force-reencode` | off | Re-encode everything |
| `--timeout SECONDS` | 86400 | Per-file encode timeout (24h default) |

## Architecture

```
opentranscode/
├── __init__.py          # Package metadata + lazy launch_gui wrapper
├── __main__.py          # python -m opentranscode entry point
├── cli.py               # argparse + dry-run + verify-only
├── codec_profiles.py    # VideoCodecProfile (incl. NVENC gpu fields), Audio/Container/Resolution tables
├── encoder_worker.py    # QThread-based per-file encode pipeline + engine (GPU/CPU) resolution
├── env_probe.py         # Distro + binary + library + av1an + NVENC probe
├── ffprobe_utils.py     # ffprobe_validate, ffprobe_duration, file-type ID
├── temp_manager.py      # Per-worker temp directory isolation
├── cpu_topology.py      # Physical core / logical thread detection
├── distro_probe.py      # Distro family + package manager detection
├── keepawake.py         # systemd-inhibit + optional mouse nudge
├── source_builder.py    # From-git rebuild for VapourSynth/av1an ABI mismatches
├── license_registry.py  # Third-party license attribution
├── ui_window.py         # PySide6 main window + launch_gui
├── ui_theme.py          # Retro-futuristic QSS theme
└── widgets/             # Custom Qt widgets (radio_knob, etc.)

open-transcode.py        # Launcher script (mirrors package, test target)
pyproject.toml           # PEP 621 build config
tests/                   # 200+ tests across 16 files
```

### Encode pipeline

1. **Environment probe** — detects distro, ffmpeg/ffprobe/av1an paths,
   encoder library availability, VapourSynth + source plugins, CPU topology
2. **Pre-flight validation** — ffprobe scans all input files; reports valid
   vs invalid counts before encoding starts
3. **Per-file pipeline**:
   - `_validate_file` — ffprobe pre-check (skip if invalid)
   - `_check_disk_space` — warn (verbose) if free space < source size
   - `_output_already_encoded` — skip if output exists with matching codec
   - `_prepare_input` — pre-scale to a CRF-16 intermediate (av1an path only)
     or symlink to temp; the ffmpeg/GPU path scales inline
   - `_encode_one` — dispatch to ffmpeg (default) or av1an (opt-in)
   - `_run_with_stop_check` — subprocess with STOP-button interrupt support
   - `_verify_and_finalize` — duration check (≥95%), subtitle mux, source deletion
4. **Final cleanup** — sweep per-worker temp dir, delete verified sources

### Thread safety

- Each `EncoderWorker` runs in its own `QThread`
- Per-worker temp subdirectory (`~/.cache/OpenTranscode/tmp/worker-<pid>/`)
  created with mode 0700 (SEI CERT FIO09-C)
- Process-group signaling (`start_new_session=True` + `os.killpg`) reaches
  av1an's child encoders (SvtAv1EncApp / vpxenc / x265)
- Drainer threads read stdout/stderr continuously to prevent pipe-buffer
  deadlock (same pattern as `subprocess.run._communicate`)

### Coding standards

The codebase follows:
- **PEP 868** — parameterized type hints (`dict[str, object]`, not `Dict[str, object]`)
- **SEI CERT** — MSC04-C (single source of truth for diagnostics), FIO09-C
  (secure temp directory), ERR01-C (narrow exception scope), STR09-C (no
  substring matches for encoder names)
- **POSIX** — `start_new_session=True` for process-group signaling,
  `signal.SIGTERM` → `SIGKILL` escalation, `os.killpg` for child cleanup
- **MISRA** (where applicable to Python) — single exit point per function
  where practical, no early returns from `try` blocks without cleanup

## Testing

```bash
python -m pytest tests/ -q          # 149 tests, ~10s
python -m pytest tests/ -v          # verbose
python -m pytest tests/ -k "skip_existing"  # subset
```

Test categories:
- **Smoke tests** — av1an VSScript compatibility probe
- **Encoder pipeline** — `_validate_file`, `_prepare_input`, `_verify_and_finalize`
- **Real-encode e2e** — generates real test videos with ffmpeg, runs the full pipeline
- **Chunk-method retry** — y4m pipe break recovery
- **Stop button** — SIGTERM/SIGKILL on process group
- **Concurrent workers** — per-PID temp directory isolation
- **Skip-existing** — codec matching, ffprobe failure, resolution mismatch
- **Massive files** — timeout flag, 1KB integrity threshold, disk-space checks
- **Package structure** — public API surface, submodule imports, CLI parser

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).

Third-party tools invoked (not bundled): ffmpeg, ffprobe, av1an,
VapourSynth, SvtAv1EncApp, vpxenc, x265, mkvmerge. Licenses flow
through from upstream.

## Project

- **Repository**: https://git.dcos.net/dcosnet/OpenTranscode
- **Issues**: https://git.dcos.net/dcosnet/OpenTranscode/issues
- **Changelog**: [CHANGELOG.md](CHANGELOG.md)
