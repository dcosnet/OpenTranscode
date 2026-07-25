"""SourceBuildWorker (QThread) — builds VS / av1an / ffmpeg from git.

Resolves VapourSynth/av1an ABI mismatches by compiling the affected
components from source. Installs to the user's home dir (no sudo for
the install step). v3-08 made this worker stop mutating
``os.environ`` directly — it carries its own ``_build_env`` snapshot.

Pure stdlib + PySide6 (no internal package dependencies).
"""

import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal

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
                 build_ffmpeg_iamf: bool = False):
        super().__init__()
        self.build_vs = build_vs
        self.build_av1an = build_av1an
        self.build_ffmpeg_iamf = build_ffmpeg_iamf
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
            # ── Install build dependencies (may need one sudo prompt) ──
            self.log_msg.emit("")
            self.log_msg.emit("=== Installing build dependencies ===")
            all_deps = [
                "meson", "ninja", "gcc", "pkg-config", "git",
                "nasm", "yasm", "cmake", "python", "make",
            ]
            need_rust = self.build_av1an and not shutil.which("cargo")
            if need_rust:
                all_deps.append("rust")

            # Only invoke sudo if at least one dep is missing
            missing = [d for d in all_deps if not shutil.which(d)]
            if missing:
                self.log_msg.emit(f"  Missing: {', '.join(missing)} — installing via pacman")
                rc, _ = self._sudo_cmd(
                    ["pacman", "-S", "--needed", "--noconfirm"] + all_deps,
                    timeout=300, label="pacman build-deps",
                )
                if rc != 0:
                    self.log_msg.emit("  (some deps may already be installed — continuing)")
            else:
                self.log_msg.emit("  All build dependencies already installed.")

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

            # ── Build av1an to ~/.cargo/bin (NO sudo needed) ──
            if self.build_av1an:
                self._build_av1an()

            # ── Build libiamf + ffmpeg with --enable-libiamf to ~/.local ──
            if self.build_ffmpeg_iamf:
                self._build_libiamf()
                self._build_ffmpeg_with_iamf()

            # ── Ensure LD_LIBRARY_PATH includes local VS libs ──
            local_lib = str(Path.home() / ".local" / "lib")
            existing_ld = self._build_env.get("LD_LIBRARY_PATH", "")
            if local_lib not in existing_ld:
                self._extend_env("LD_LIBRARY_PATH", local_lib, prepend=True)
                self.log_msg.emit(f"  Set LD_LIBRARY_PATH to include {local_lib}")

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

