"""SourceBuildWorker (QThread) — builds VS / av1an / ffmpeg from git.

Resolves VapourSynth/av1an ABI mismatches by compiling the affected
components from source. Installs to the user's home dir (no sudo for
the install step). v3-08 made this worker stop mutating
``os.environ`` directly — it carries its own ``_build_env`` snapshot.

v4.7.1: the rebuild is ALWAYS usable, even on a bare system. It
generates its own dependency tree: missing build tools and libraries
are installed via the distro package manager (arch/debian/redhat/suse)
before anything is compiled, and the VapourSynth build is followed by
a BestSource plugin build (submodules + vapoursynth dev headers from
the freshly installed VS) so av1an gets a fast, reliable chunk method
instead of the slow "select" fallback.
"""

import os
import re
import shutil
import signal
import site
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .distro_probe import DistroProfile, detect_distro
from .gpu_profiles import gpu_profile_by_key

# ──────────────────────────────────────────────
#  BUILD DEPENDENCY TREE (v4.7.1 — distro-aware)
# ──────────────────────────────────────────────

# Binaries the build needs. pkgconf/pkg-config and python/python3 are
# aliased — any one of each pair satisfies the check.
BUILD_TOOL_ALIASES: dict[str, tuple[str, ...]] = {
    "git": ("git",),
    "meson": ("meson",),
    "ninja": ("ninja",),
    "c++ compiler": ("g++", "c++", "clang++"),
    "make": ("make",),
    "pkg-config": ("pkg-config", "pkgconf"),
    "python3": ("python3",),
    "nasm": ("nasm",),
    "cmake": ("cmake",),
}

# Packages providing the toolchain + the libraries the builds link
# against (zimg is VapourSynth's one hard library dependency; rust is
# only needed for the av1an build).
BUILD_DEPS_BY_FAMILY: dict[str, list[str]] = {
    "arch":   ["base-devel", "meson", "ninja", "cmake", "nasm", "git",
               "python", "pkgconf", "zimg", "rust"],
    "debian": ["build-essential", "meson", "ninja-build", "cmake", "nasm",
               "git", "python3", "python3-dev", "pkg-config", "libzimg-dev",
               "cargo", "rustc"],
    "redhat": ["gcc", "gcc-c++", "make", "meson", "ninja-build", "cmake",
               "nasm", "git", "python3", "python3-devel",
               "pkgconf-pkg-config", "zimg-devel", "cargo", "rust"],
    "suse":   ["gcc", "gcc-c++", "make", "meson", "ninja", "cmake", "nasm",
               "git", "python3", "python3-devel", "pkg-config",
               "zimg-devel", "rust", "cargo"],
}

PKG_INSTALL_CMD: dict[str, list[str]] = {
    "arch":   ["pacman", "-S", "--needed", "--noconfirm"],
    "debian": ["apt-get", "install", "-y"],
    "redhat": ["dnf", "install", "-y"],
    "suse":   ["zypper", "--non-interactive", "install"],
}

MANUAL_DEP_NOTE = (
    "No automatic package install for this distro family. Install a C++ "
    "toolchain plus meson, ninja, cmake, nasm, git, python3, pkg-config, "
    "zimg development headers{rust} manually, then press REBUILD again."
)

# v4.8.0: per-GPU-profile build/runtime packages, on top of the base
# toolchain. nvidia needs nv-codec-headers at ffmpeg build time (the
# distro ffmpeg already ships nvenc; a matched git build needs the
# headers); vaapi/qsv need the driver + dev stacks for their vendor.
GPU_BUILD_PACKAGES: dict[str, dict[str, list[str]]] = {
    "nvenc": {
        "arch":   ["nv-codec-headers"],
        "debian": [],
        "redhat": [],
        "suse":   [],
    },
    "vaapi": {
        "arch":   ["libva", "libdrm", "mesa"],
        "debian": ["libva-dev", "libdrm-dev", "mesa-va-drivers"],
        "redhat": ["libva-devel", "libdrm-devel", "mesa-va-drivers"],
        "suse":   ["libva-devel", "libdrm-devel", "Mesa-libva"],
    },
    "qsv": {
        "arch":   ["libva", "intel-media-driver", "onevpl"],
        "debian": ["libva-dev", "intel-media-va-driver-non-free", "libvpl-dev"],
        "redhat": ["libva-devel", "intel-media-driver", "oneVPL-devel"],
        "suse":   ["libva-devel", "intel-media-driver", "oneVPL-devel"],
    },
}


def gpu_dep_packages(api: str, distro_family: str) -> list[str]:
    """Extra packages for a GPU hardware API on this distro (empty when
    the family has no packaged set — the log says so)."""
    return list(GPU_BUILD_PACKAGES.get(api, {}).get(distro_family, []))


@dataclass
class DepPlan:
    """What the rebuild needs, and how to get it on this distro."""
    packages: list[str] = field(default_factory=list)
    install_cmd: list[str] | None = None
    manual_note: str | None = None


def build_dep_plan(distro: DistroProfile, build_av1an: bool = True) -> DepPlan:
    """Pure: the package list + install command for this distro family.

    Works even when the environment probe failed — it only needs the
    distro family, which is detectable from /etc/os-release alone.
    """
    packages = list(BUILD_DEPS_BY_FAMILY.get(distro.family, []))
    if not build_av1an:
        for rust_pkg in ("rust", "rustc", "cargo"):
            if rust_pkg in packages:
                packages.remove(rust_pkg)
    install_cmd = PKG_INSTALL_CMD.get(distro.family)
    manual_note = None
    if not install_cmd or not packages:
        manual_note = MANUAL_DEP_NOTE.format(
            rust=" and Rust/cargo" if build_av1an else "")
    return DepPlan(packages=packages, install_cmd=install_cmd,
                   manual_note=manual_note)

# ──────────────────────────────────────────────
#  SOURCE BUILD WORKER — compile VS + av1an from git
# ──────────────────────────────────────────────

class SourceBuildWorker(QThread):
    """Builds VapourSynth and/or av1an from git to resolve ABI mismatches.

    Runs in a background thread.  Emits progress via log_msg.
    When done, emits build_done(success, message).

    Everything installs to the user's home directory (no sudo for install):
      VapourSynth → ~/.local/lib/  (av1an finds it via LD_LIBRARY_PATH)
      av1an       → ~/.cargo/bin/   (already in PATH)
    Only build-dependency installation (pacman -S) may need sudo.
    """
    log_msg    = Signal(str)
    build_done = Signal(bool, str)   # (success, detail)

    def __init__(self, build_vs: bool = True, build_av1an: bool = True,
                 build_ffmpeg_iamf: bool = False, gpu_profile_key: str = ""):
        super().__init__()
        self.build_vs = build_vs
        self.build_av1an = build_av1an
        self.build_ffmpeg_iamf = build_ffmpeg_iamf
        # v4.8.0: selected GPU capability profile — extends the dep tree
        # with the vendor's build/runtime packages.
        self.gpu_profile_key = gpu_profile_key
        self._stop = False
        # Private per-worker environment snapshot. Mutating os.environ is
        # process-global and leaks across threads/subsequent subprocesses;
        # _build_env is local to this worker and passed via env= to every
        # subprocess.run call below (see _run_cmd).
        self._build_env: dict[str, str] = os.environ.copy()

    def _extend_env(self, var: str, value: str, prepend: bool = False):
        """Add ``value`` to ``self._build_env[var]`` (NOT ``os.environ``).

        ``prepend=True`` places ``value`` first so it shadows any existing
        entry (e.g. ~/.local/bin must shadow /usr/bin, libiamf's
        PKG_CONFIG_PATH must shadow the system pkgconfig dir); default
        appends (e.g. extending PATH with ~/.cargo/bin). Caller is
        responsible for any idempotency check (matches the original
        per-site ``if x not in existing:`` pattern). rstrip(":") on
        prepend avoids a trailing colon when ``var`` was previously unset.
        """
        existing = self._build_env.get(var, "")
        if prepend:
            self._build_env[var] = f"{value}:{existing}".rstrip(":")
        else:
            self._build_env[var] = f"{existing}:{value}" if existing else value

    def _run_cmd(self, cmd, cwd=None, timeout=600, label=""):
        """Run a command, log output, return (returncode, combined_output)."""
        self.log_msg.emit(f"  $ {' '.join(cmd[:6])}{'...' if len(cmd)>6 else ''}")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout, cwd=cwd, env=self._build_env)
            # Log last few lines of stderr for diagnostics
            if r.stderr:
                for line in r.stderr.strip().splitlines()[-5:]:
                    self.log_msg.emit(f"    {line}")
            if r.returncode != 0 and r.stdout:
                for line in r.stdout.strip().splitlines()[-3:]:
                    self.log_msg.emit(f"    {line}")
            return r.returncode, (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            self.log_msg.emit(f"  TIMEOUT ({timeout}s) running: {label or cmd[0]}")
            return -1, f"timeout after {timeout}s"
        except (OSError, subprocess.SubprocessError) as e:
            self.log_msg.emit(f"  ERROR: {e}")
            return -1, str(e)

    def _sudo_cmd(self, cmd, timeout=120, label=""):
        """Run a command with sudo (or pkexec as graphical fallback)."""
        # Try pkexec first (graphical polkit prompt — works in desktop sessions)
        pkexec = shutil.which("pkexec")
        if pkexec:
            return self._run_cmd([pkexec] + cmd, timeout=timeout, label=label or cmd[0])
        # Fall back to sudo (needs a terminal; may fail silently)
        return self._run_cmd(["sudo"] + cmd, timeout=timeout, label=label or cmd[0])

    def run(self):
        try:
            # ── v4.7.1: distro-aware dependency tree ──
            # The rebuild must work on a bare system: detect missing
            # tools/libraries and install them via the distro package
            # manager (one privilege prompt via pkexec/sudo) BEFORE
            # compiling anything.
            self.log_msg.emit("")
            self.log_msg.emit("=== Generating dependency tree ===")
            self._distro = detect_distro()
            self.log_msg.emit(
                f"  Distro: {self._distro.name} (family={self._distro.family})"
            )
            plan = build_dep_plan(self._distro, build_av1an=self.build_av1an)

            # v4.8.0: GPU-profile packages on top of the base toolchain.
            gpu_profile = gpu_profile_by_key(self.gpu_profile_key)
            if gpu_profile is not None and gpu_profile.api != "none":
                gpu_pkgs = gpu_dep_packages(gpu_profile.api, self._distro.family)
                if gpu_pkgs:
                    self.log_msg.emit(
                        f"  GPU profile {gpu_profile.key} ({gpu_profile.api}): "
                        f"+{len(gpu_pkgs)} package(s)"
                    )
                    plan.packages.extend(p for p in gpu_pkgs
                                         if p not in plan.packages)

            missing = self._missing_build_tools()
            zimg_ok = self._pkgconfig_exists("zimg")
            if zimg_ok:
                self.log_msg.emit("  OK: zimg (VapourSynth dependency)")
            else:
                missing.append("zimg (library, via pkg-config)")
            if self.build_av1an and not shutil.which("cargo"):
                missing.append("cargo (rust)")

            if missing:
                self.log_msg.emit(f"  Missing: {', '.join(missing)}")
                if plan.install_cmd:
                    self.log_msg.emit(
                        f"  Installing {len(plan.packages)} package(s) via "
                        f"{plan.install_cmd[0]} (privilege prompt possible)..."
                    )
                    rc, _ = self._sudo_cmd(
                        plan.install_cmd + plan.packages,
                        timeout=900, label=f"{plan.install_cmd[0]} build-deps",
                    )
                    if rc != 0:
                        self.log_msg.emit(
                            "  (install reported an error — continuing; "
                            "some packages may already be present)"
                        )
                else:
                    self.log_msg.emit(f"  {plan.manual_note}")
            else:
                self.log_msg.emit("  All build dependencies already installed.")

            # Re-verify the critical tools after install.
            still_missing = self._missing_build_tools()
            if still_missing:
                self.log_msg.emit(
                    f"  FATAL: still missing after install: {', '.join(still_missing)}. "
                    f"Install them manually and press REBUILD again."
                )
                self.build_done.emit(False, f"missing build tools: {still_missing}")
                return

            # Ensure cargo is in PATH after potential install.
            # NOTE: /root/.cargo/bin was dropped (OTC-015/v3-08) — root's
            # cargo dir is not readable by a non-root user. ~/.cargo/bin
            # covers the user's rustup install; /usr/bin is already in the
            # default PATH and is appended here only to match the original
            # mutation's intent (cargo from pacman lives there).
            self._extend_env("PATH", "/usr/bin")
            self._extend_env("PATH", str(Path.home() / ".cargo" / "bin"))
            if not shutil.which("cargo") and self.build_av1an:
                self.log_msg.emit("  FATAL: cargo not found after deps install. Aborting.")
                self.build_done.emit(False, "Rust/cargo not available")
                return

            # ── Optional: ffmpeg build deps (libopus, libvorbis dev pkgs) ──
            if self.build_ffmpeg_iamf:
                self._install_ffmpeg_build_deps()

            # ── Build & install VapourSynth to ~/.local (NO sudo needed) ──
            if self.build_vs:
                self._build_vapoursynth()
                # v4.7.1: BestSource right after VS, compiled against the
                # fresh VS headers — gives av1an a fast chunk method.
                self._build_bestsource()

            # ── Build av1an to ~/.cargo/bin (NO sudo needed) ──
            if self.build_av1an:
                self._build_av1an()

            # ── Build libiamf + ffmpeg with --enable-libiamf to ~/.local ──
            if self.build_ffmpeg_iamf:
                self._build_libiamf()
                self._build_ffmpeg_with_iamf()

            # ── Ensure the runtime env can find the fresh VS stack ──
            # The git VapourSynth installs self-contained into the user
            # site-packages (module + libs + plugins). av1an dlopens
            # libvapoursynth-script from there, so both LD_LIBRARY_PATH
            # and PYTHONPATH must include it.
            local_lib = str(Path.home() / ".local" / "lib")
            existing_ld = self._build_env.get("LD_LIBRARY_PATH", "")
            if local_lib not in existing_ld:
                self._extend_env("LD_LIBRARY_PATH", local_lib, prepend=True)
            user_site = self._vs_user_site()
            if user_site and (user_site / "vapoursynth" / "libvsscript.so").exists():
                vs_dir = str(user_site / "vapoursynth")
                if vs_dir not in self._build_env.get("LD_LIBRARY_PATH", ""):
                    self._extend_env("LD_LIBRARY_PATH", vs_dir, prepend=True)
                if str(user_site) not in self._build_env.get("PYTHONPATH", ""):
                    self._extend_env("PYTHONPATH", str(user_site), prepend=True)
                self.log_msg.emit(
                    f"  Runtime env: LD_LIBRARY_PATH/PYTHONPATH include {vs_dir}"
                )

            self.log_msg.emit("")
            self.log_msg.emit("=== Source build complete ===")
            self.build_done.emit(True, "Build and install completed (local ~/.local/).")

        except Exception as e:
            # SEI CERT ERR01-C: justified — this method orchestrates a long
            # multi-step build (git clone, meson, ninja, cargo install) whose
            # helper methods signal failure by `raise Exception(msg)` (15
            # sites). Catching Exception here converts any of those into a
            # user-facing build_done(False, ...) signal instead of crashing
            # the QThread. Narrowing would require refactoring all `raise
            # Exception(...)` call sites — out of scope for ERR01-C pass.
            self.log_msg.emit(f"BUILD FAILED: {e}")
            self.build_done.emit(False, str(e))

    def _missing_build_tools(self) -> list[str]:
        """Binaries from BUILD_TOOL_ALIASES that are not on PATH."""
        missing = []
        for label, candidates in BUILD_TOOL_ALIASES.items():
            if not any(shutil.which(c) for c in candidates):
                missing.append(label)
        return missing

    def _pkgconfig_exists(self, name: str) -> bool:
        rc, _ = self._run_cmd(
            ["pkg-config", "--exists", name],
            timeout=10, label=f"pkg-config {name}",
        )
        return rc == 0

    def _vs_user_site(self) -> Path | None:
        """The user site-packages dir of the system python3 — where the
        VapourSynth git install places its self-contained stack (module,
        libs, headers, plugins/)."""
        rc, out = self._run_cmd(
            ["python3", "-m", "site", "--user-site"],
            timeout=15, label="python3 -m site --user-site",
        )
        if rc == 0 and out.strip():
            return Path(out.strip().splitlines()[-1])
        return None

    def _build_bestsource(self):
        """Clone and build the BestSource VapourSynth plugin from git.

        BestSource gives av1an a fast, frame-accurate chunk source — the
        difference between 'select' (quadratic decoding, minutes per
        file) and normal chunk-parallel speed. Compiled against the
        vapoursynth headers of the JUST-INSTALLED git VS (via
        PYTHONPATH/PKG_CONFIG_PATH), so the plugin ABI always matches
        the VS that av1an will load. Requires the repo's libp2p
        submodule (initialized here).
        """
        self.log_msg.emit("")
        self.log_msg.emit("=== Building BestSource plugin from git ===")
        self.log_msg.emit("  Source: https://github.com/vapoursynth/bestsource")

        build_dir = Path("/tmp/bestsource-git-build")
        if build_dir.exists():
            shutil.rmtree(build_dir, ignore_errors=True)

        rc, out = self._run_cmd(
            ["git", "clone", "--depth", "1",
             "https://github.com/vapoursynth/bestsource.git",
             str(build_dir)],
            timeout=120, label="git clone bestsource",
        )
        if rc != 0:
            raise Exception(f"git clone bestsource failed: {out[-300:]}")

        # libp2p is a required submodule (R9+ builds source from it).
        rc, out = self._run_cmd(
            ["git", "submodule", "update", "--init", "--depth", "1"],
            cwd=str(build_dir), timeout=120, label="git submodule update",
        )
        if rc != 0:
            raise Exception(f"bestsource submodule init failed: {out[-300:]}")

        # Point meson/pkg-config at the freshly built VS stack.
        user_site = self._vs_user_site()
        if user_site and (user_site / "vapoursynth").is_dir():
            self._extend_env("PYTHONPATH", str(user_site), prepend=True)
            self._extend_env("PKG_CONFIG_PATH",
                             str(user_site / "vapoursynth" / "pkgconfig"),
                             prepend=True)
            self._extend_env("LD_LIBRARY_PATH",
                             str(user_site / "vapoursynth"), prepend=True)
        else:
            self.log_msg.emit(
                "  NOTE: git VapourSynth install not found in user "
                "site-packages — building against system vapoursynth."
            )

        self.log_msg.emit("  Configuring with meson (--prefix=~/.local)...")
        rc, out = self._run_cmd(
            ["meson", "setup", "build",
             f"--prefix={Path.home() / '.local'}", "--libdir=lib"],
            cwd=str(build_dir), timeout=180, label="meson setup bestsource",
        )
        if rc != 0:
            raise Exception(f"bestsource meson setup failed: {out[-500:]}")

        self.log_msg.emit("  Compiling BestSource (a minute or two)...")
        rc, out = self._run_cmd(
            ["ninja", "-C", "build", "-j", str(max(1, os.cpu_count() or 2))],
            cwd=str(build_dir), timeout=600, label="ninja bestsource",
        )
        if rc != 0:
            raise Exception(f"bestsource build failed: {out[-500:]}")

        rc, out = self._run_cmd(
            ["ninja", "-C", "build", "install"],
            cwd=str(build_dir), timeout=120, label="ninja install bestsource",
        )
        if rc != 0:
            raise Exception(f"bestsource install failed: {out[-500:]}")

        plugin = None
        if user_site:
            candidate = user_site / "vapoursynth" / "plugins" / "libbestsource.so"
            if candidate.exists():
                plugin = candidate
        if plugin:
            self.log_msg.emit(f"  BestSource plugin installed: {plugin}")
            self.log_msg.emit(
                "  av1an will now auto-select the fast 'bestsource' chunk "
                "method (restart the app so the probe sees it)."
            )
        else:
            self.log_msg.emit(
                "  WARNING: libbestsource.so not found at the expected "
                "user-site path — check the meson install log above."
            )

        shutil.rmtree(build_dir, ignore_errors=True)

    def _build_vapoursynth(self):
        """Clone, build, and install VapourSynth to ~/.local/ (no sudo needed)."""
        self.log_msg.emit("")
        self.log_msg.emit("=== Building VapourSynth from git ===")
        self.log_msg.emit("  Install target: ~/.local/ (no system-wide changes)")
        build_dir = Path("/tmp/vapoursynth-git-build")
        local_prefix = str(Path.home() / ".local")

        if build_dir.exists():
            self.log_msg.emit(f"  Cleaning old build directory...")
            shutil.rmtree(build_dir, ignore_errors=True)

        # Clone (shallow — faster)
        rc, out = self._run_cmd(
            ["git", "clone", "--depth", "1",
             "https://github.com/vapoursynth/vapoursynth.git",
             str(build_dir)],
            timeout=120, label="git clone vapoursynth",
        )
        if rc != 0:
            raise Exception(f"git clone VapourSynth failed: {out[-300:]}")

        # Meson setup — install to ~/.local so it doesn't touch system dirs
        self.log_msg.emit("  Configuring with meson (--prefix=~/.local)...")
        rc, out = self._run_cmd(
            ["meson", "setup", "build",
             f"--prefix={local_prefix}", "--libdir=lib"],
            cwd=str(build_dir), timeout=120, label="meson setup",
        )
        if rc != 0:
            raise Exception(f"meson setup failed: {out[-500:]}")

        # Build
        self.log_msg.emit("  Compiling VapourSynth (this may take a few minutes)...")
        rc, out = self._run_cmd(
            ["ninja", "-C", "build", "-j", str(max(1, os.cpu_count() or 2))],
            cwd=str(build_dir), timeout=900, label="ninja build",
        )
        if rc != 0:
            raise Exception(f"ninja build failed: {out[-500:]}")

        # Install to ~/.local/ — NO sudo needed (user owns this directory)
        self.log_msg.emit("  Installing VapourSynth to ~/.local/ ...")
        rc, out = self._run_cmd(
            ["ninja", "-C", "build", "install"],
            cwd=str(build_dir), timeout=120, label="ninja install",
        )
        if rc != 0:
            raise Exception(f"ninja install failed: {out[-500:]}")

        self.log_msg.emit(f"  VapourSynth installed to {local_prefix}/ (libs in {local_prefix}/lib/)")

        # Cleanup build directory
        shutil.rmtree(build_dir, ignore_errors=True)

    def _build_av1an(self):
        """Clone and build av1an from git. Installs to ~/.cargo/bin/ (no sudo needed)."""
        self.log_msg.emit("")
        self.log_msg.emit("=== Building av1an from git ===")
        self.log_msg.emit("  Install target: ~/.cargo/bin/ (no system-wide changes)")

        # Ensure cargo is in PATH
        cargo_bin = shutil.which("cargo")
        if not cargo_bin:
            # Common locations
            for p in [Path.home() / ".cargo" / "bin" / "cargo", "/usr/bin/cargo"]:
                if p.exists():
                    self._extend_env("PATH", str(p.parent))
                    cargo_bin = str(p)
                    break
        if not cargo_bin:
            raise Exception("cargo not found — cannot build av1an")

        self.log_msg.emit(f"  Using cargo at: {cargo_bin}")
        self.log_msg.emit("  Compiling av1an (this may take 10-30 minutes)...")

        rc, out = self._run_cmd(
            ["cargo", "install", "av1an",
             "--git", "https://github.com/master-of-zen/av1an",
             "--force", "--root", str(Path.home() / ".cargo")],
            timeout=3600, label="cargo install av1an",
        )
        if rc != 0:
            raise Exception(f"cargo install av1an failed: {out[-500:]}")

        new_av1an = Path.home() / ".cargo" / "bin" / "av1an"
        if new_av1an.exists():
            self.log_msg.emit(f"  av1an installed: {new_av1an}")
        else:
            self.log_msg.emit("  WARNING: av1an binary not found at expected path after build.")

    def _install_ffmpeg_build_deps(self):
        """Install ffmpeg build deps (libopus, libvorbis dev packages).

        Uses pkg-config to detect missing libraries, then installs the
        corresponding Arch/pacman packages. On other distros the user
        must install these manually; the log will name them.
        """
        self.log_msg.emit("")
        self.log_msg.emit("=== Checking ffmpeg build dependencies ===")

        # (pkg-config name, Arch package name, Debian package name)
        pkg_checks = [
            ("opus",   "opus",      "libopus-dev"),
            ("vorbis", "libvorbis", "libvorbis-dev"),
            ("ogg",    "libogg",    "libogg-dev"),
        ]
        missing_arch = []
        missing_debian = []
        for pc_name, arch_pkg, debian_pkg in pkg_checks:
            rc, _ = self._run_cmd(
                ["pkg-config", "--exists", pc_name],
                timeout=10, label=f"pkg-config {pc_name}",
            )
            if rc != 0:
                missing_arch.append(arch_pkg)
                missing_debian.append(debian_pkg)
                self.log_msg.emit(f"  Missing: {arch_pkg} (pkg-config {pc_name})")
            else:
                self.log_msg.emit(f"  OK: {pc_name}")

        if not missing_arch:
            self.log_msg.emit("  All ffmpeg build deps satisfied.")
            return

        # Try pacman (Arch) first since the rest of this app assumes Arch
        if shutil.which("pacman"):
            self.log_msg.emit(f"  Installing via pacman: {', '.join(missing_arch)}")
            rc, _ = self._sudo_cmd(
                ["pacman", "-S", "--needed", "--noconfirm"] + missing_arch,
                timeout=300, label="pacman ffmpeg-deps",
            )
            if rc != 0:
                self.log_msg.emit("  WARNING: pacman install failed — configure may fail.")
        elif shutil.which("apt-get"):
            self.log_msg.emit(f"  Installing via apt: {', '.join(missing_debian)}")
            rc, _ = self._sudo_cmd(
                ["apt-get", "install", "-y"] + missing_debian,
                timeout=300, label="apt ffmpeg-deps",
            )
            if rc != 0:
                self.log_msg.emit("  WARNING: apt install failed — configure may fail.")
        else:
            self.log_msg.emit(
                f"  No supported package manager found. Install manually: "
                f"{', '.join(missing_arch)} (Arch) or {', '.join(missing_debian)} (Debian)."
            )

    def _build_libiamf(self):
        """Clone, build, and install libiamf to ~/.local/ (no sudo needed).

        libiamf is the AOMedia Immersive Audio Model and Formats reference
        library. ffmpeg links against it via --enable-libiamf.
        """
        self.log_msg.emit("")
        self.log_msg.emit("=== Building libiamf from git ===")
        self.log_msg.emit("  Source: https://github.com/AOMediaCodec/libiamf")
        self.log_msg.emit("  Install target: ~/.local/ (no system-wide changes)")

        build_dir = Path("/tmp/libiamf-git-build")
        local_prefix = Path.home() / ".local"

        if build_dir.exists():
            shutil.rmtree(build_dir, ignore_errors=True)

        # Clone (shallow)
        self.log_msg.emit("  Cloning libiamf source (shallow)...")
        rc, out = self._run_cmd(
            ["git", "clone", "--depth", "1",
             "https://github.com/AOMediaCodec/libiamf.git",
             str(build_dir)],
            timeout=120, label="git clone libiamf",
        )
        if rc != 0:
            raise Exception(f"git clone libiamf failed: {out[-300:]}")

        # CMake configure
        cmake_build = build_dir / "build"
        cmake_build.mkdir(exist_ok=True)
        self.log_msg.emit(f"  Configuring with cmake (--prefix={local_prefix})...")
        rc, out = self._run_cmd(
            ["cmake", "-S", str(build_dir), "-B", str(cmake_build),
             f"-DCMAKE_INSTALL_PREFIX={local_prefix}",
             "-DCMAKE_BUILD_TYPE=Release",
             "-DBUILD_SHARED_LIBS=ON"],
            timeout=120, label="cmake configure libiamf",
        )
        if rc != 0:
            raise Exception(f"cmake configure libiamf failed:\n{out[-500:]}")

        # Build
        self.log_msg.emit("  Compiling libiamf...")
        rc, out = self._run_cmd(
            ["cmake", "--build", str(cmake_build), "-j",
             str(max(1, os.cpu_count() or 2))],
            timeout=600, label="cmake build libiamf",
        )
        if rc != 0:
            raise Exception(f"cmake build libiamf failed:\n{out[-500:]}")

        # Install
        self.log_msg.emit(f"  Installing libiamf to {local_prefix}/ ...")
        rc, out = self._run_cmd(
            ["cmake", "--install", str(cmake_build)],
            timeout=120, label="cmake install libiamf",
        )
        if rc != 0:
            raise Exception(f"cmake install libiamf failed:\n{out[-500:]}")

        # Make libiamf discoverable: PKG_CONFIG_PATH and LD_LIBRARY_PATH
        pc_dir = local_prefix / "lib" / "pkgconfig"
        if pc_dir.exists():
            existing_pkgs = self._build_env.get("PKG_CONFIG_PATH", "")
            if str(pc_dir) not in existing_pkgs:
                self._extend_env("PKG_CONFIG_PATH", str(pc_dir), prepend=True)
                self.log_msg.emit(f"  Added {pc_dir} to PKG_CONFIG_PATH")

        lib_dir = local_prefix / "lib"
        existing_ld = self._build_env.get("LD_LIBRARY_PATH", "")
        if str(lib_dir) not in existing_ld:
            self._extend_env("LD_LIBRARY_PATH", str(lib_dir), prepend=True)

        self.log_msg.emit(f"  libiamf installed to {local_prefix}/")

        # Cleanup
        shutil.rmtree(build_dir, ignore_errors=True)

    def _build_ffmpeg_with_iamf(self):
        """Rebuild ffmpeg from source with libiamf (and IAMF's Opus dep).

        Strategy: detect the current ffmpeg's --enable-* configure flags,
        reuse them, and append --enable-libiamf. This preserves all
        existing functionality (libsvtav1, libvpx, libx265, etc.) while
        adding IAMF support.

        Installs to ~/.local/bin/ffmpeg so it shadows the system ffmpeg
        without overwriting it. The user must restart the app for the
        new ffmpeg to take effect (probe_environment re-runs on launch).
        """
        self.log_msg.emit("")
        self.log_msg.emit("=== Building ffmpeg from git with IAMF ===")
        self.log_msg.emit("  Install target: ~/.local/bin/ (shadows system ffmpeg)")

        # 1. Detect current ffmpeg configure flags
        ffmpeg_bin = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
        self.log_msg.emit(f"  Probing current ffmpeg config: {ffmpeg_bin}")
        rc, out = self._run_cmd(
            [ffmpeg_bin, "-buildconf"],
            timeout=30, label="ffmpeg -buildconf",
        )
        if rc != 0:
            raise Exception(f"ffmpeg -buildconf failed:\n{out[-300:]}")

        # Parse --enable-* flags from output (one per line, sometimes with leading whitespace)
        enables = re.findall(r"--enable-[a-z0-9_-]+", out)
        # Dedupe while preserving order
        seen = set()
        enable_flags = []
        for e in enables:
            if e not in seen:
                seen.add(e)
                enable_flags.append(e)

        # Make sure libiamf and libopus are in the list (core requirements)
        if "--enable-libiamf" not in enable_flags:
            enable_flags.append("--enable-libiamf")
        if "--enable-libopus" not in enable_flags:
            enable_flags.append("--enable-libopus")

        self.log_msg.emit(f"  Configure flags ({len(enable_flags)}):")
        for f in enable_flags:
            self.log_msg.emit(f"    {f}")

        # 2. Clone ffmpeg source
        build_dir = Path("/tmp/ffmpeg-git-build")
        if build_dir.exists():
            shutil.rmtree(build_dir, ignore_errors=True)

        self.log_msg.emit("  Cloning ffmpeg source (shallow)...")
        rc, out = self._run_cmd(
            ["git", "clone", "--depth", "1",
             "https://git.ffmpeg.org/ffmpeg.git",
             str(build_dir)],
            timeout=300, label="git clone ffmpeg",
        )
        if rc != 0:
            # Fall back to GitHub mirror
            self.log_msg.emit("  Primary mirror failed, trying github mirror...")
            rc, out = self._run_cmd(
                ["git", "clone", "--depth", "1",
                 "https://github.com/FFmpeg/FFmpeg.git",
                 str(build_dir)],
                timeout=300, label="git clone ffmpeg (github)",
            )
            if rc != 0:
                raise Exception(f"git clone ffmpeg failed:\n{out[-300:]}")

        local_prefix = Path.home() / ".local"

        # Make sure pkg-config finds the freshly-built libiamf
        pc_dir = local_prefix / "lib" / "pkgconfig"
        existing_pkgs = self._build_env.get("PKG_CONFIG_PATH", "")
        if str(pc_dir) not in existing_pkgs:
            self._extend_env("PKG_CONFIG_PATH", str(pc_dir), prepend=True)

        # 3. Configure
        self.log_msg.emit("  Running ./configure (this may take a minute)...")
        configure_cmd = [
            "./configure",
            f"--prefix={local_prefix}",
            "--enable-shared",
            "--enable-pic",
            "--enable-version3",
        ] + enable_flags

        rc, out = self._run_cmd(
            configure_cmd,
            cwd=str(build_dir), timeout=300, label="ffmpeg configure",
        )
        if rc != 0:
            # Show the actual error — usually a missing -dev package
            raise Exception(
                "ffmpeg configure failed. This usually means a dev library\n"
                "is missing. Install the corresponding -dev package and retry.\n"
                f"Output:\n{out[-800:]}"
            )

        # 4. Build
        self.log_msg.emit("  Compiling ffmpeg (this may take 10-20 minutes)...")
        rc, out = self._run_cmd(
            ["make", "-j", str(max(1, os.cpu_count() or 2))],
            cwd=str(build_dir), timeout=2400, label="make ffmpeg",
        )
        if rc != 0:
            raise Exception(f"ffmpeg make failed:\n{out[-500:]}")

        # 5. Install to ~/.local
        self.log_msg.emit(f"  Installing ffmpeg to {local_prefix}/ ...")
        rc, out = self._run_cmd(
            ["make", "install"],
            cwd=str(build_dir), timeout=300, label="make install ffmpeg",
        )
        if rc != 0:
            raise Exception(f"make install ffmpeg failed:\n{out[-500:]}")

        # 6. Ensure ~/.local/bin is in PATH so new ffmpeg shadows system one
        local_bin = local_prefix / "bin"
        existing_path = self._build_env.get("PATH", "")
        if str(local_bin) not in existing_path:
            self._extend_env("PATH", str(local_bin), prepend=True)
            self.log_msg.emit(f"  Prepended {local_bin} to PATH (shadows system ffmpeg)")

        new_ffmpeg = local_bin / "ffmpeg"
        if new_ffmpeg.exists():
            self.log_msg.emit(f"  ffmpeg installed: {new_ffmpeg}")
            self.log_msg.emit(
                "  IMPORTANT: Restart the app for the new ffmpeg (with libiamf)\n"
                "  to be detected and used. The IAMF audio entry will then\n"
                "  be selectable (not greyed out)."
            )
        else:
            self.log_msg.emit("  WARNING: ffmpeg binary not found at expected path after build.")

        # Cleanup build dir (keep source for re-runs? No — disk is cheap, time isn't, but
        # a clean clone is more reliable than a stale tree.)
        shutil.rmtree(build_dir, ignore_errors=True)

    def stop(self):
        self._stop = True

