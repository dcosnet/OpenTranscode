"""Environment probe — distro-aware binary + library + av1an detection.

Combines the distro probe (``detect_distro``) and the CPU topology
probe (``detect_cpu_topology``) with binary path search, ffmpeg
library availability probing, av1an version/flag probing, runtime
dependency probing, and the av1an VSScript smoke test.

The smoke-test helpers (``_av1an_env``, ``_av1an_vsscript_smoke_test``,
``_detect_av1an_svt_encoder``) live here rather than in
``ffprobe_utils`` because they exercise av1an (not ffprobe) and are
called from both the GUI (``ui_window``) and the CLI dry-run
(``cli.run_dry_run``).
"""

import ctypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .cpu_topology import CpuTopology, detect_cpu_topology
from .distro_probe import DistroProfile, detect_distro

# ──────────────────────────────────────────────
#  ENVIRONMENT PROBE (distro-aware, extended)
# ──────────────────────────────────────────────

@dataclass
class EnvProbe:
    distro: DistroProfile = field(default_factory=lambda: DistroProfile(
        family="unknown", name="Unknown", version_id="?",
        pkg_manager="unknown", install_cmd_template="",
        binary_extra_paths=[], av1an_known_encoder_names=[],
        ffmpeg_pkg="ffmpeg", av1an_pkg="av1an", notes=""
    ))
    av1an_path: str | None = None
    ffmpeg_path: str | None = None
    ffprobe_path: str | None = None
    # v3 (OTC-011, PEP 868): parameterized dict/list type hints.
    # av1an_flags values are sometimes str (flag name), sometimes bool
    # (has_chunk_method), sometimes int — keep as dict[str, object] for honesty.
    av1an_flags: dict[str, object] = field(default_factory=dict)
    av1an_version: str | None = None
    ffmpeg_version: str | None = None
    ffmpeg_libs: dict[str, bool] = field(default_factory=dict)  # lib name -> available
    runtime_deps: dict[str, bool] = field(default_factory=dict)  # dep name -> present
    missing_dep_pkgs: list[str] = field(default_factory=list)  # distro pkg names to install
    vs_version: str | None = None  # VapourSynth version string (for diagnostics)
    vs_script_lib: str | None = None  # path to libvapoursynth-script.so that passed
    cpu: CpuTopology = field(default_factory=lambda: CpuTopology(1, 1, 1, "Unknown"))
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return (self.av1an_path is not None and self.ffmpeg_path is not None
                and not self.errors and not self.missing_dep_pkgs)

    @property
    def dep_install_hint(self) -> str:
        """Generate a distro-specific install command for missing runtime deps."""
        if not self.missing_dep_pkgs or self.distro.family == "unknown":
            return ""
        return self.distro.install_cmd_template.format(packages=" ".join(self.missing_dep_pkgs))

    @property
    def install_hint(self) -> str:
        """Generate a distro-specific install command for missing packages."""
        missing = []
        if self.av1an_path is None:
            missing.append(self.distro.av1an_pkg)
        if self.ffmpeg_path is None:
            missing.append(self.distro.ffmpeg_pkg)
        if not missing:
            return ""
        return self.distro.install_cmd_template.format(packages=" ".join(missing))


def _find_binary(name: str, distro: DistroProfile) -> str | None:
    """
    Search for a binary in: (1) standard PATH via shutil.which, then
    (2) distro-specific extra paths (expanded ~). Returns first match.
    """
    # Standard PATH search
    found = shutil.which(name)
    if found:
        return found

    # Distro-specific extra paths
    for raw_path in distro.binary_extra_paths:
        expanded = Path(raw_path).expanduser()
        candidate = expanded / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    return None


def _probe_ffmpeg_libs(ffmpeg_bin: str) -> dict[str, bool]:
    """Check which encoder/decoder libraries ffmpeg was compiled with.
    Runs ffmpeg -encoders ONCE and greps for all known encoder names.
    Each entry: (key, [search_strings]) — any match = available.

    v4 STABILITY FIX: the v3 search strings for libsvtav1 and libaom were
    wrong. ffmpeg's `-encoders` output lists them as `libsvtav1` and
    `libaom-av1` (no underscore between svt/av1, hyphen between aom/av1) —
    NOT `libsvt_av1` / `libaom_av1`. This caused _probe_ffmpeg_libs to
    report False for both even when they were installed, which then caused
    _handle_vs_incompat to incorrectly tell the user "ffmpeg also lacks
    libsvtav1" and abort — even though ffmpeg actually had it. The e2e
    test test_probe_detects_ffmpeg_libs caught this.
    """
    try:
        res = subprocess.run(
            [ffmpeg_bin, "-encoders"],
            capture_output=True, text=True, timeout=10,
        )
        output = res.stdout
    except (OSError, subprocess.SubprocessError):
        output = ""

    # v4: search strings match the EXACT names ffmpeg -encoders prints.
    # Verified against ffmpeg 7.x output:
    #   V..... libsvtav1            SVT-AV1(...) encoder (codec av1)
    #   V....D libaom-av1           libaom AV1 (codec av1)
    #   V....D libvpx-vp9           libvpx VP9 (codec vp9)
    # The trailing space in each search string anchors the match to the
    # encoder name boundary, preventing false positives like "libvpx_vp9"
    # matching "libvpx_vp9_decoder" (which doesn't exist, but defensive).
    # We also include the underscore variant as a fallback for older
    # ffmpeg builds that may have used that spelling.
    checks = [
        ("libsvtav1", ["libsvtav1 ", "libsvt_av1", "svt_av1 "]),
        ("libaom",    ["libaom-av1 ", "libaom_av1", "aom_av1 "]),
        ("libvpx",    ["libvpx-vp9 ", "libvpx_vp9", "vpx_vp9 "]),
        ("libx265",   ["libx265 "]),
        ("libopus",   ["libopus "]),
        ("libvorbis", ["libvorbis "]),
        ("flac",      ["flac "]),
    ]
    libs = {}
    for lib_name, search_strings in checks:
        libs[lib_name] = any(s in output for s in search_strings)
    return libs


def _probe_av1an_version(av1an_bin: str) -> str | None:
    """Extract av1an version string."""
    try:
        # Try --version first, fall back to parsing --help header
        for args in (["--version"], ["--help"]):
            res = subprocess.run(
                [av1an_bin] + args,
                capture_output=True, text=True, timeout=10,
            )
            output = res.stdout or res.stderr
            match = re.search(r"av1an\s+([\d.]+(?:-\w+)?)", output, re.IGNORECASE)
            if match:
                return match.group(1)
            if res.stdout.strip():  # If --version produced output but no version match
                return res.stdout.strip().splitlines()[0][:60]
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _probe_ffmpeg_version(ffmpeg_bin: str) -> str | None:
    """Extract ffmpeg version string."""
    try:
        res = subprocess.run(
            [ffmpeg_bin, "-version"],
            capture_output=True, text=True, timeout=10,
        )
        first_line = res.stdout.splitlines()[0] if res.stdout else ""
        match = re.search(r"ffmpeg version (\S+)", first_line)
        return match.group(1) if match else first_line[:60]
    except (OSError, subprocess.SubprocessError):
        return None


def _probe_runtime_deps(distro: DistroProfile) -> tuple[dict[str, bool], list[str]]:
    """Check runtime dependencies that av1an needs to function.
    Returns (deps_dict, missing_pkg_names).
    
    Checks:
      - VapourSynth + VSScript (av1an loads libvapoursynth-script.so via dlopen
        to get the VSScript API — without this it panics with
        'Failed to get VSScript API')
      - Encoder binaries that av1an invokes directly (svt_av1, x265, vpxenc)
    """
    deps: dict[str, bool] = {}
    missing_pkgs: list[str] = []

    # --- VapourSynth + VSScript (critical: av1an will panic without it) ---
    # av1an is a Rust binary that dlopen's libvapoursynth-script.so and calls
    # vsscript_init() / vsscript_createScript() / etc.  It does NOT use the
    # Python vapoursynth module.  The shared library and the VSScript API
    # library can be packaged separately on some distros (e.g. Debian has
    # libvapoursynth-script-dev).  We must check what av1an actually loads.
    #
    # IMPORTANT: We do NOT call vsscript_init() in our probe.  VSScript's init
    # internally calls Py_Initialize(), which crashes/fails when Python is
    # already running (our probe runs inside a Python subprocess).  Instead,
    # we verify the shared library exists AND can be dlopen'd (CDLL constructor
    # resolves all .so dependencies).  If it loads, it will work for av1an.
    vs_ok = False
    vs_detail = ""
    vs_ver_str = ""
    vs_lib_path = None

    # --- Step 1: Direct filesystem check (most reliable) ---
    # Check well-known install paths. Works even if ldconfig cache is stale.
    _vs_script_search = [
        "/usr/lib/libvapoursynth-script.so",
        "/usr/lib/libvapoursynth_script.so",
        "/usr/lib64/libvapoursynth-script.so",
        "/usr/lib/x86_64-linux-gnu/libvapoursynth-script.so",
        "/usr/local/lib/libvapoursynth-script.so",
    ]
    for p in _vs_script_search:
        if Path(p).is_file():
            vs_lib_path = p
            break

    # --- Step 2: Glob search on known lib dirs ---
    if not vs_lib_path:
        for lib_dir in ("/usr/lib", "/usr/lib64", "/usr/local/lib",
                        "/usr/lib/x86_64-linux-gnu"):
            d = Path(lib_dir)
            if d.is_dir():
                matches = list(d.glob("libvapoursynth-script.so*"))
                # Prefer unversioned .so over .so.0 (dev symlink)
                for m in sorted(matches, key=lambda p: p.name):
                    vs_lib_path = str(m)
                    break
                if vs_lib_path:
                    break

    # --- Step 3: ldconfig -p ---
    if not vs_lib_path:
        try:
            res = subprocess.run(
                ["ldconfig", "-p"], capture_output=True, text=True, timeout=5,
            )
            for line in res.stdout.splitlines():
                if "libvapoursynth-script" in line or "libvapoursynth_script" in line:
                    parts = line.split("=>")
                    if len(parts) >= 2:
                        vs_lib_path = parts[1].strip().split()[0]
                        break
        except (OSError, subprocess.SubprocessError):
            pass

    # --- Step 4: ctypes.util.find_library ---
    if not vs_lib_path:
        try:
            for name in ("vapoursynth-script", "vapoursynth_script"):
                found = ctypes.util.find_library(name)
                if found:
                    vs_lib_path = found
                    break
        except (OSError, subprocess.SubprocessError):
            pass

    # --- Step 5: Distro-specific package file listing ---
    if not vs_lib_path:
        pkg_query = {
            "arch":   ["pacman", "-Ql", "vapoursynth"],
            "debian": ["dpkg", "-L", "vapoursynth"],
            "redhat": ["rpm", "-ql", "vapoursynth"],
            "suse":   ["rpm", "-ql", "vapoursynth"],
        }
        query_cmd = pkg_query.get(distro.family)
        if query_cmd:
            try:
                res = subprocess.run(
                    query_cmd, capture_output=True, text=True, timeout=10,
                )
                for line in res.stdout.splitlines():
                    line = line.strip()
                    # Skip directory entries and grab .so files
                    if "libvapoursynth-script" in line and line.endswith(".so"):
                        vs_lib_path = line
                        break
                    if "libvapoursynth-script" in line and ".so." in line and not vs_lib_path:
                        vs_lib_path = line  # versioned .so as fallback
            except (OSError, subprocess.SubprocessError):
                pass

    # --- Step 6: dlopen smoke test (diagnostic only, NOT a gate) ---
    # We do NOT gate on dlopen success.  The library's constructor may call
    # Py_Initialize() which conflicts with our Python subprocess, causing a
    # silent segfault.  av1an loads this library in its own fresh Rust process
    # where no Python is running — so it works there even if our probe crashes.
    # We only use dlopen to produce an optional warning.
    vs_dlopen_warning = ""
    if vs_lib_path:
        try:
            _escaped = vs_lib_path.replace("'", "\\'")
            probe_code = (
                "import ctypes; "
                f"try: h = ctypes.CDLL('{_escaped}'); print('LOAD_OK') "
                f"except OSError as e: print(f'LOAD_FAIL|{{e}}') "
                f"except Exception as e: print(f'LOAD_OTHER|{{e}}') "
            )
            res = subprocess.run(
                [sys.executable, "-c", probe_code],
                capture_output=True, text=True, timeout=10,
            )
            out = res.stdout.strip()
            if out == "LOAD_OK":
                vs_ok = True
            elif out:
                vs_dlopen_warning = f"dlopen test failed: {out}"
                vs_ok = True  # file exists — let av1an try in its own process
            else:
                # subprocess produced no output — likely segfault in library
                # constructor (Py_Initialize conflict). File still exists.
                vs_dlopen_warning = "dlopen test produced no output (likely segfault in library constructor — not a problem for av1an)"
                vs_ok = True
        except subprocess.TimeoutExpired:
            vs_dlopen_warning = "dlopen test timed out (library may have hanging constructor)"
            vs_ok = True
        except (OSError, subprocess.SubprocessError) as e:
            vs_dlopen_warning = f"dlopen probe error: {e}"
            vs_ok = True

    # Final gate: library file was found on disk
    if vs_lib_path and not vs_ok:
        vs_ok = True  # file found on disk is sufficient

    if vs_ok:
        vs_detail = vs_lib_path or "found"
        # Try to get VapourSynth version from the core lib for diagnostics
        try:
            ver_probe = (
                "import ctypes, ctypes.util; "
                "_lib = ctypes.util.find_library('vapoursynth'); "
                "if not _lib: "
                "  import subprocess as _sp; "
                "  _r = _sp.run(['ldconfig','-p'], capture_output=True, text=True, timeout=5); "
                "  _m = [l.split('=>')[1].strip().split()[0] for l in _r.stdout.splitlines() "
                "       if 'libvapoursynth.so.' in l and 'script' not in l]; "
                "  _lib = _m[0] if _m else None; "
                "if _lib: "
                "  try: "
                "    _h = ctypes.CDLL(_lib); "
                "    _fn = _h.vapoursynth_version; "
                "    _fn.restype = ctypes.c_int; "
                "    print(_fn()) "
                "  except: pass "
            )
            res = subprocess.run(
                [sys.executable, "-c", ver_probe],
                capture_output=True, text=True, timeout=10,
            )
            ver_out = res.stdout.strip()
            if ver_out and ver_out.isdigit() and int(ver_out) > 0:
                vs_ver_str = f"R{ver_out}"
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        if not vs_detail:
            vs_detail = "libvapoursynth-script.so not found (checked filesystem, ldconfig, and package manager)"

    deps["vapoursynth"] = vs_ok
    if not vs_ok:
        # Determine which package(s) to suggest.
        # Most distros bundle VSScript into the main 'vapoursynth' package,
        # but some split it (Debian/Ubuntu: libvapoursynth-script-dev).
        # Use the dedicated vsscript_pkg field if set, else fall back to dep_pkgs.
        if distro.vsscript_pkg:
            missing_pkgs.append(distro.vsscript_pkg)
        elif "vapoursynth" in distro.dep_pkgs:
            missing_pkgs.append(distro.dep_pkgs["vapoursynth"])
        deps["vs_detail"] = False  # extra key for the diagnostic message
    else:
        deps["vs_detail"] = True

    # --- Encoder binaries (av1an invokes these directly, not via ffmpeg) ---
    for enc_key, binary_names in distro.encoder_binaries.items():
        found = False
        for bin_name in binary_names:
            if _find_binary(bin_name, distro) is not None:
                found = True
                break
        deps[enc_key] = found
        if not found:
            # Map encoder key to dep_pkgs key
            dep_key_map = {"svt_av1": "svt-av1", "vpx": "vpx", "x265": "x265"}
            dep_key = dep_key_map.get(enc_key, enc_key)
            if dep_key in distro.dep_pkgs:
                pkg_name = distro.dep_pkgs[dep_key]
                if pkg_name not in missing_pkgs:
                    missing_pkgs.append(pkg_name)

    # --- ffprobe (needed for input file validation) ---
    # Already checked in probe_environment() for the main binary, but let's
    # make sure the dep dict reflects it for consistency.
    # (ffprobe_path is set separately in probe_environment)

    return deps, missing_pkgs, vs_detail, vs_ver_str, vs_dlopen_warning


def probe_environment() -> EnvProbe:
    """
    Distro-aware binary detection + av1an flag compatibility probe +
    ffmpeg library availability check.
    """
    distro = detect_distro()
    result = EnvProbe(distro=distro)
    result.cpu = detect_cpu_topology()
    cpu = result.cpu

    result.warnings.append(f"Detected distro: {distro.name} (family={distro.family}, v{distro.version_id})")
    result.warnings.append(
        f"CPU: {cpu.model_name} — {cpu.physical_cores} physical cores x {cpu.threads_per_core} threads = {cpu.logical_threads} logical"
    )

    # --- Binary detection (distro-aware path search) ---
    for name, attr in [("av1an", "av1an_path"), ("ffmpeg", "ffmpeg_path"), ("ffprobe", "ffprobe_path")]:
        path = _find_binary(name, distro)
        if path is None:
            result.errors.append(f"Missing binary: {name}")
        else:
            setattr(result, attr, path)

    # --- Install hint for missing binaries ---
    if result.install_hint:
        result.warnings.append(f"Install command: {result.install_hint}")

    # --- FFmpeg version + library probe ---
    if result.ffmpeg_path:
        result.ffmpeg_version = _probe_ffmpeg_version(result.ffmpeg_path)
        if result.ffmpeg_version:
            result.warnings.append(f"FFmpeg version: {result.ffmpeg_version}")
        result.ffmpeg_libs = _probe_ffmpeg_libs(result.ffmpeg_path)

        # Warn about missing AUDIO libs (video codecs are handled by av1an's own
        # encoder binaries — ffmpeg's video encoder list is irrelevant)
        audio_lib_warnings = {
            "Opus": "libopus",
            "Vorbis": "libvorbis",
            "FLAC": "flac",
        }
        for codec_label, lib_name in audio_lib_warnings.items():
            if not result.ffmpeg_libs.get(lib_name, False):
                result.warnings.append(f"FFmpeg missing encoder: {lib_name} ({codec_label} audio will not work)")

    # --- Av1an version ---
    if result.av1an_path:
        result.av1an_version = _probe_av1an_version(result.av1an_path)
        if result.av1an_version:
            result.warnings.append(f"av1an version: {result.av1an_version}")

    # --- Av1an flag compatibility probe ---
    if result.av1an_path:
        try:
            help_out = subprocess.run(
                [result.av1an_path, "--help"],
                capture_output=True, text=True, timeout=15,
            ).stdout
            result.av1an_flags = {
                "worker": "--workers" if "--workers" in help_out else "-w",
                "video_params": "--video-params" if "--video-params" in help_out else "-v",
                "audio_params": "--audio-params" if "--audio-params" in help_out else "-a",
            }

            # Detect which encoder names this av1an build actually accepts.
            # Substring matching on --help is unreliable (e.g. "svt" appears in
            # descriptions but the real name may be "svtav1" or "svt_av1").
            # Instead, pass a bogus encoder name and parse the clap error which
            # lists all valid values.
            svt_name = _detect_av1an_svt_encoder(result.av1an_path)
            if svt_name:
                result.av1an_flags["svt_name"] = svt_name
                result.warnings.append(f"av1an SVT-AV1 encoder name: '{svt_name}'")
            else:
                # Absolute fallback — should rarely be needed
                result.av1an_flags["svt_name"] = "svt_av1"
                result.warnings.append("av1an SVT-AV1 encoder name: 'svt_av1' (fallback, not auto-detected)")

            # Check for chunk-method availability (differs by av1an version/distro)
            if "--chunk-method" in help_out:
                result.av1an_flags["has_chunk_method"] = True

            # Check for --temp flag (lets us relocate av1an work dir out of user folders)
            if "--temp" in help_out:
                result.av1an_flags["has_temp"] = True
                result.av1an_flags["temp_flag"] = "--temp"
            elif "-T" in help_out:
                result.av1an_flags["has_temp"] = True
                result.av1an_flags["temp_flag"] = "-T"

            # Check for -s/segments flag (newer av1an)
            if "-s" in help_out or "--scenes" in help_out:
                result.av1an_flags["has_scenes"] = True

            # Detect concat method: prefer mkvmerge, fall back to ffmpeg
            if shutil.which("mkvmerge"):
                result.av1an_flags["concat_method"] = "mkvmerge"
            else:
                result.av1an_flags["concat_method"] = "ffmpeg"

            # v4.0.0: Probe VapourSynth source plugins. When NONE of the
            # source plugins (lsmash, ffms2, bestsource, dgdecnv) are
            # installed, av1an falls back to the Hybrid chunk method —
            # which fails on phone-recorded MP4s with sparse keyframes
            # (the "works up until near the end, never saves chunks into
            # a full file" bug). Pre-setting chunk_method_override="select"
            # avoids the wasted first-attempt + retry on every file.
            #
            # The select method uses VapourSynth's select() filter to
            # extract frames one-by-one — slower than ffms2/bestsource
            # but reliable for any file VapourSynth can open.
            vs_plugins = _probe_vs_source_plugins()
            result.av1an_flags["vs_plugins"] = vs_plugins
            if vs_plugins:
                result.warnings.append(
                    f"VapourSynth source plugins: {', '.join(vs_plugins)} "
                    f"— av1an will auto-select a fast chunk method"
                )
            else:
                result.warnings.append(
                    "VapourSynth source plugins: NONE found — "
                    "forcing --chunk-method select (reliable but slower). "
                    "Install vapoursynth-{lsmash,ffms2,bestsource} for faster "
                    "chunk-parallel encoding."
                )
                result.av1an_flags["chunk_method_override"] = "select"

        except (OSError, subprocess.SubprocessError) as e:
            result.errors.append(f"av1an probe failed: {e}")

    # --- Distro-specific notes ---
    if distro.notes:
        result.warnings.append(f"Distro note: {distro.notes}")

    # --- Runtime dependency probe (vapoursynth, encoder binaries) ---
    if result.av1an_path:
        deps, missing_pkgs, vs_detail, vs_ver, vs_dlopen_warn = _probe_runtime_deps(distro)
        result.runtime_deps = deps
        result.missing_dep_pkgs = missing_pkgs
        if deps.get("vapoursynth"):
            result.vs_version = vs_ver
            result.vs_script_lib = vs_detail

        # Log VapourSynth/VSScript with extra detail
        vs_status = "OK" if deps.get("vapoursynth") else "MISSING"
        result.warnings.append(f"Dependency: vapoursynth (VSScript API) = {vs_status}")
        if deps.get("vapoursynth"):
            # vs_detail is the library path on success
            result.warnings.append(f"  VSScript lib: {vs_detail}")
            if vs_dlopen_warn:
                result.warnings.append(f"  dlopen note: {vs_dlopen_warn}")
        else:
            # vs_detail is the failure reason
            result.warnings.append(f"  Reason: {vs_detail}")

        # Log encoder binary deps (skip vs_detail key)
        for dep_name, present in deps.items():
            if dep_name in ("vapoursynth", "vs_detail"):
                continue
            status = "OK" if present else "MISSING"
            result.warnings.append(f"Dependency: {dep_name} = {status}")

        if missing_pkgs:
            hint = result.dep_install_hint
            result.errors.append(
                f"Missing runtime dependencies: {', '.join(missing_pkgs)}"
            )
            if hint:
                result.errors.append(f"  FIX: {hint}")

    return result



def _detect_av1an_svt_encoder(av1an_bin: str) -> str | None:
    """Determine the exact encoder name av1an accepts for SVT-AV1.

    Strategy (in order):
      1. Run ``av1an --encoder __PROBE__`` and parse clap's error for
         ``[possible values: ...]``.
      2. Parse ``--help`` for ``[default: <name>]`` next to ``--encoder``.
      3. Regex fallback on the error output.
    """
    try:
        # --- Method 1: clap error with possible values ---
        res = subprocess.run(
            [av1an_bin, "--encoder", "__PROBE_TEST__"],
            capture_output=True, text=True, timeout=10,
        )
        stderr = res.stderr or ""
        stdout = res.stdout or ""
        combined = stderr + stdout

        m = re.search(r"\[possible values:\s*([^\]]+)\]", combined)
        if m:
            values = [v.strip().rstrip(',') for v in m.group(1).split()]
            for v in values:
                if "svt" in v.lower():
                    return v

        # --- Method 2: parse --help for encoder default value ---
        help_res = subprocess.run(
            [av1an_bin, "--help"],
            capture_output=True, text=True, timeout=10,
        )
        help_text = (help_res.stdout or "") + (help_res.stderr or "")
        # Look for pattern: --encoder <ENCODER> ... [default: svt-av1]
        m2 = re.search(
            r"--encoder\s+<ENCODER>.*?\[default:\s*(\S+?)\]",
            help_text, re.DOTALL,
        )
        if m2:
            return m2.group(1)

        # --- Method 3: regex fallback on the error output ---
        for line in combined.splitlines():
            for token in re.findall(r"\bsvt[a-z_-]*av1[a-z_-]*\b", line, re.IGNORECASE):
                return token
            for token in re.findall(r"\bsvtav1\b", line, re.IGNORECASE):
                return token

        return None
    except (OSError, subprocess.SubprocessError):
        return None


def _av1an_env() -> dict[str, str]:
    """Build an env dict for subprocess that includes ~/.local/lib in LD_LIBRARY_PATH.

    When VapourSynth is built from git and installed to ~/.local/, the linker
    won't find libvapoursynth-script.so unless LD_LIBRARY_PATH points there.
    This function ensures every av1an invocation inherits that path.
    """
    env = os.environ.copy()
    local_lib = str(Path.home() / ".local" / "lib")
    existing = env.get("LD_LIBRARY_PATH", "")
    if local_lib not in existing:
        env["LD_LIBRARY_PATH"] = f"{local_lib}:{existing}".rstrip(":")
    return env


# v4.0.0: VapourSynth source plugin probe. Returns a list of available
# plugin names (e.g. ["lsmash", "ffms2", "bestsource"]). When the list
# is empty, av1an falls back to the Hybrid chunk method — which fails
# on phone-recorded MP4s with sparse keyframes. The caller uses this
# to decide whether to pre-set chunk_method_override="select".
_VS_PLUGIN_PROBE_PATHS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # (plugin_name, candidate .so filenames)
    # lsmash: imported as `havsfmt` / `lsmas` in VS; .so is libvslsmashsource.so
    ("lsmash",     ("libvslsmashsource.so",)),
    # ffms2: imported as `ffms2` in VS; .so is libffms2.so (sometimes libvffms2.so)
    ("ffms2",      ("libffms2.so", "libvffms2.so")),
    # bestsource: imported as `bestsource` / `bs` in VS
    ("bestsource", ("libbestsource.so", "libvsbestsource.so")),
    # dgdecnv: NVIDIA hardware-accelerated decoder
    ("dgdecnv",    ("libdgdecnv.so",)),
    # vszip: high-performance resize/format plugins
    ("vszip",      ("libvszip.so",)),
)


def _probe_vs_source_plugins() -> list[str]:
    """Probe for VapourSynth source plugins in standard locations.

    Searches (in order):
      1. ``$XDG_DATA_HOME/vapoursynth/`` (or ``~/.local/share/vapoursynth/``)
      2. ``~/.local/lib/vapoursynth/`` (user-installed plugins from source)
      3. ``/usr/lib/vapoursynth/`` (distro-installed plugins)
      4. ``/usr/local/lib/vapoursynth/`` (manually installed)
      5. ``/usr/lib/x86_64-linux-gnu/vapoursynth/`` (Debian multiarch)

    Returns a sorted list of available plugin names. Empty list = no
    source plugins found, which means av1an will fall back to Hybrid
    chunk method and likely fail on phone-recorded MP4s.

    Pure-stdlib (no vapoursynth Python bindings required). Best-effort:
    if a plugin is installed but not in these paths, this probe will
    miss it — but the av1an runtime will still detect it, and the
    v4.0.0 retry in _encode_one will still switch to select on first
    failure.
    """
    search_dirs: list[Path] = []
    xdg_data = os.environ.get("XDG_DATA_HOME", "")
    if xdg_data:
        search_dirs.append(Path(xdg_data) / "vapoursynth")
    else:
        search_dirs.append(Path.home() / ".local" / "share" / "vapoursynth")
    search_dirs.append(Path.home() / ".local" / "lib" / "vapoursynth")
    search_dirs.append(Path("/usr/lib/vapoursynth"))
    search_dirs.append(Path("/usr/local/lib/vapoursynth"))
    search_dirs.append(Path("/usr/lib/x86_64-linux-gnu/vapoursynth"))

    found: set[str] = set()
    for d in search_dirs:
        if not d.is_dir():
            continue
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_file():
                continue
            name_lower = entry.name.lower()
            for plugin_name, so_names in _VS_PLUGIN_PROBE_PATHS:
                for so_name in so_names:
                    if so_name in name_lower:
                        found.add(plugin_name)
                        break

    return sorted(found)


def _av1an_vsscript_smoke_test(
    av1an_bin: str,
    ffmpeg_bin: str,
    av1an_flags: dict,
    svt_name: str = "svt_av1",
    timeout: int = 30,
) -> tuple[bool, str]:
    """Pre-flight test: create a tiny video and try to run av1an on it.

    This catches 'Failed to get VSScript API' panics BEFORE the real queue
    starts.  File-existence checks for libvapoursynth-script.so pass even
    when the ABI is incompatible (av1an's Rust vapoursynth crate built
    against a different VS version).  Only actually invoking av1an reveals
    the mismatch.

    Returns (ok, detail_message).
      ok=True   -> av1an initialized VSScript successfully.
      ok=False  -> av1an panicked or failed; detail_message explains why.
    """

    with tempfile.TemporaryDirectory(prefix="av1an_smoke_") as tmpdir:
        test_in = Path(tmpdir) / "test_smoke.mkv"
        test_out = Path(tmpdir) / "test_smoke_out.mkv"

        # Create a 1-second 64x64 black video (video-only is enough to
        # trigger VSScript init in av1an — no audio needed).
        gen_cmd = [
            ffmpeg_bin,
            "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1:r=24",
            "-t", "1", "-pix_fmt", "yuv420p", "-an", "-y", str(test_in),
        ]
        try:
            res = subprocess.run(gen_cmd, capture_output=True, text=True, timeout=15)
            if res.returncode != 0:
                return False, f"ffmpeg test-video failed (rc={res.returncode}): {(res.stderr or '')[-200:]}"
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"Could not generate smoke test video: {e}"

        if not test_in.exists():
            return False, "Smoke test video was not created by ffmpeg"

        # Build minimal av1an command
        worker_flag = av1an_flags.get("worker", "--workers")
        vparams_flag = av1an_flags.get("video_params", "--video-params")
        aparams_flag = av1an_flags.get("audio_params", "--audio-params")

        cmd = [
            av1an_bin,
            "-i", str(test_in),
            worker_flag, "1",
            "--encoder", svt_name,
            vparams_flag, "--preset 8 --crf 40 --keyint 240",
            "-o", str(test_out),
        ]

        # Use chunk-method select if available (triggers VSScript init)
        if av1an_flags.get("has_chunk_method"):
            cmd.extend(["--chunk-method", "select"])

        # SEI CERT ERR01-C: catch only the specific exception types we
        # expect from subprocess.run; never swallow unrelated failures.
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                env=_av1an_env())
        except subprocess.TimeoutExpired:
            # Timeout is a real failure — av1an is hanging. Do NOT mask it.
            return False, f"SMOKE_TIMEOUT: av1an smoke test exceeded {timeout}s — likely hung in VSScript init or encoder spawn"
        except FileNotFoundError as e:
            return False, f"SMOKE_BIN_MISSING: {e}"
        except OSError as e:
            return False, f"SMOKE_OS_ERROR: {e}"

        stderr = res.stderr or ""
        stdout = res.stdout or ""

        # Success requires BOTH rc==0 AND the output file actually exists.
        # The previous code returned True on any non-VSScript failure, which
        # masked real bugs (missing encoder binary, concat failure, etc.)
        # and led to "chunks but never saves a file" symptoms in production.
        if res.returncode == 0 and test_out.exists():
            test_out.unlink(missing_ok=True)
            return True, "av1an VSScript init OK"

        # Classify the known failure modes by inspecting stderr.
        if "Failed to get VSScript API" in stderr:
            return False, "VSScript_API_INCOMPAT"

        if "invalid value" in stderr and "--encoder" in stderr:
            return False, f"INVALID_ENCODER: {stderr[-200:]}"

        if "No usable encoder found" in stderr:
            return False, f"ENCODER_BIN_MISSING: {stderr[-300:]}"

        # Unknown failure — return False so the caller can offer ffmpeg
        # fallback or rebuild. Include the FULL stderr (not just the tail)
        # so the user can see the actual error and the diagnostic patterns
        # below can match on it.
        combined = (stderr + "\n--- stdout ---\n" + stdout)[-1500:]
        return False, f"SMOKE_FAIL(rc={res.returncode}): {combined}"

