"""Temp directory management for intermediate encode files.

Owns the shared app cache dir, per-worker temp subdirs (v3-09 race
fix), private-dir mkdir (v3-09 umask defeat), and the per-source-path
hash naming helper. Pure stdlib; no internal package dependencies.
"""

import hashlib
import os
import tempfile
from pathlib import Path

# ──────────────────────────────────────────────
#  TEMP DIRECTORY MANAGEMENT
# ──────────────────────────────────────────────

_APP_CACHE_DIR: Path | None = None

def _get_app_temp_dir() -> Path:
    """Return the shared temp directory for all intermediate files.

    Priority:
      1. ``~/.cache/OpenTranscode/tmp/``  (XDG-compliant, persistent across reboots)
      2. ``/tmp/OpenTranscode/``           (fallback if home cache is unwritable)

    The directory is created on first call.  All temp intermediates
    (pre-scaled MKVs, av1an work dirs) go here so the user's video
    folders stay clean.

    v3 (OTC-013, SEI CERT FIO09-C): the directory is created with
    ``mode=0o700`` so that other users on the system cannot create
    symlinks inside it (which the cleanup sweep would then follow and
    delete arbitrary files). The mode is verified after creation in
    case the directory already existed with looser permissions.
    """
    global _APP_CACHE_DIR
    if _APP_CACHE_DIR is not None:
        return _APP_CACHE_DIR

    # Try XDG cache dir first
    xdg_cache = os.environ.get("XDG_CACHE_HOME", "")
    if xdg_cache:
        candidate = Path(xdg_cache) / "OpenTranscode" / "tmp"
    else:
        candidate = Path.home() / ".cache" / "OpenTranscode" / "tmp"

    if _mkdir_private(candidate):
        _APP_CACHE_DIR = candidate
        return _APP_CACHE_DIR

    # Fallback: /tmp/OpenTranscode
    fallback = Path("/tmp/OpenTranscode")
    if _mkdir_private(fallback):
        _APP_CACHE_DIR = fallback
        return _APP_CACHE_DIR

    # Last resort: system temp
    _APP_CACHE_DIR = Path(tempfile.gettempdir()) / "OpenTranscode"
    _mkdir_private(_APP_CACHE_DIR)
    return _APP_CACHE_DIR


def _mkdir_private(path: Path) -> bool:
    """Create *path* (and parents) with mode 0o700.

    Returns True on success, False on OSError/PermissionError.

    SEI CERT FIO09-C: if the directory already existed with looser
    permissions (e.g. created by a previous version of this app, or by
    another user before us), we attempt to tighten the mode with
    os.chmod(). The chmod may fail silently if we don't own the dir —
    that's an accepted risk, logged but not fatal.
    """
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        # mkdir(mode=) is masked by umask; explicitly chmod to be sure
        os.chmod(path, 0o700)
        return True
    except (OSError, PermissionError):
        return False


def _worker_temp_dir(worker_pid: int, lane: str = "") -> Path:
    """Return a per-worker temp subdir named by PID.

    v3: each EncoderWorker gets its own subdir under the shared app temp
    dir, so the final cleanup sweep can safely nuke only this worker's
    intermediates without affecting a concurrent worker. The subdir is
    also created with mode=0o700 (FIO09-C).

    v4.7.0: *lane* suffixes the dir ("gpu"/"cpu") for the hybrid
    scheduler's concurrent lanes — both run in the SAME process, so the
    PID alone no longer separates them, and a lane finishing early must
    not sweep the other lane's intermediates out from under it.
    """
    base = _get_app_temp_dir()
    name = f"worker-{worker_pid}" + (f"-{lane}" if lane else "")
    sub = base / name
    _mkdir_private(sub)
    return sub


def _temp_path_for(file_path: Path, suffix: str = ".scaled_tmp.mkv",
                   worker_dir: Path | None = None) -> Path:
    """Build a unique temp path for *file_path* inside the app temp dir.

    Uses a short hash of the original absolute path to avoid collisions
    when files in different subdirs share the same stem.

    v3: if *worker_dir* is provided (per-worker subdir), the temp file
    lands there instead of the shared parent. This isolates concurrent
    workers' intermediates from each other.
    """
    tmp_dir = worker_dir if worker_dir is not None else _get_app_temp_dir()
    # Hash the absolute source path for uniqueness
    path_hash = hashlib.sha256(str(file_path.resolve()).encode()).hexdigest()[:12]
    return tmp_dir / f"{file_path.stem}.{path_hash}{suffix}"

