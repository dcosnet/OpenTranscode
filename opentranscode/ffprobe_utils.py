"""ffprobe-backed validation and measurement helpers.

Three free functions:
  - ``ffprobe_validate``        — full stream-info JSON for a file.
  - ``ffprobe_duration``        — duration in seconds (or None).
  - ``_verify_output_resolution``— post-encode resolution check.
  - ``_identify_file_type``     — `file -b` output for a path (v5-03).

Pure stdlib (subprocess + json + shutil); no internal package dependencies.

v5-03: added ``_identify_file_type`` for invalid-file diagnostics.
"""

import json
import shutil
import subprocess
from pathlib import Path

# ──────────────────────────────────────────────
#  FFPREPBE VALIDATION
# ──────────────────────────────────────────────

def ffprobe_validate(filepath: Path, ffprobe_bin: str) -> dict[str, object] | None:
    """Returns stream info dict or None if invalid/unreadable."""
    try:
        res = subprocess.run(
            [ffprobe_bin, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(filepath)],
            capture_output=True, text=True, timeout=30,
        )
        if res.returncode != 0:
            return None
        return json.loads(res.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        # ValueError covers json.JSONDecodeError
        return None


def ffprobe_duration(filepath: Path, ffprobe_bin: str) -> float | None:
    """Return media duration in seconds via ffprobe, or None on failure.

    Used by the EncoderWorker post-encode integrity check to compare source
    and output durations. Modeled after :func:`ffprobe_validate` — every
    failure path returns ``None`` so the caller can treat unverifiable
    durations as "skip the check" rather than crashing the worker thread.
    """
    try:
        res = subprocess.run(
            [ffprobe_bin, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_entries", "format=duration",
             str(filepath)],
            capture_output=True, text=True, timeout=10,
        )
        if res.returncode != 0 or not res.stdout:
            return None
        data = json.loads(res.stdout)
        dur_str = (data.get("format") or {}).get("duration")
        if dur_str is None:
            return None
        return float(dur_str)
    except (OSError, subprocess.SubprocessError, ValueError):
        # ValueError covers json.JSONDecodeError and float() parse failures
        return None


def _verify_output_resolution(output_path: Path, ffprobe_bin: str, target_w: int, target_h: int) -> bool:
    """Verify that an encoded file actually has the requested output resolution.

    Returns True if the output matches (or is within 2px due to force_divisible_by=2),
    False otherwise.
    """
    try:
        res = subprocess.run(
            [ffprobe_bin, "-v", "quiet", "-print_format", "json",
             "-show_streams", "-select_streams", "v:0", str(output_path)],
            capture_output=True, text=True, timeout=15,
        )
        if res.returncode != 0:
            return True  # can't verify, don't block
        data = json.loads(res.stdout)
        streams = data.get("streams", [])
        if not streams:
            return True
        ow = int(streams[0].get("width", 0) or 0)
        oh = int(streams[0].get("height", 0) or 0)
        # Allow 2px tolerance (force_divisible_by=2 rounding)
        if abs(ow - target_w) <= 2 and abs(oh - target_h) <= 2:
            return True
        return False
    except (OSError, subprocess.SubprocessError, ValueError):
        # ValueError covers json.JSONDecodeError and int() parse failures
        return True  # can't verify, don't block


def _identify_file_type(file_path: Path) -> str:
    """Run `file` on the given path and return the type string.

    v5-03: Used by _validate_file to tell the user WHAT a file actually is
    when ffprobe can't read it. This immediately reveals:
      - "HTML document" -> failed yt-dlp download (YouTube error page saved as .mp4)
      - "ASCII text"    -> same as above (different yt-dlp version)
      - "data"          -> truncated, encrypted, or partial download
      - "ISO Media, MP4 Base Media v1" -> valid MP4 that ffprobe just can't parse (rare)

    Returns the first line of `file` output (minus the filename prefix),
    or an empty string if `file` is not available or fails.
    """
    file_bin = shutil.which("file")
    if not file_bin:
        return ""
    try:
        res = subprocess.run(
            [file_bin, "-b", str(file_path)],
            capture_output=True, text=True, timeout=5,
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""

