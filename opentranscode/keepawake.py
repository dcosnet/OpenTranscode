"""Anti-sleep / anti-hibernate subsystem (v6-06).

Keeps the system awake during long transcodes using two complementary
approaches:

1. **systemd-inhibit** (preferred, available on all systemd Linux distros):
   Runs a "fork bomb" — a no-op child process held open for the duration
   of the transcode. systemd sees the inhibit handle and will NOT suspend
   or hibernate the system while it's active. This is the cleanest
   approach: no mouse movement, no screen-lock interference, no user
  -visible side effects.

2. **Periodic mouse nudge** (fallback / belt-and-suspenders):
   If ``xdotool`` is available, moves the mouse 1 pixel every 60 seconds
   (jitter, not constant movement — the user can still click STOP or
   close the window). This catches DEs that ignore systemd-inhibit
   (rare) and prevents screen-blanking timeouts. The movement is
   minimal: +1px right, then -1px left on the next tick, so the cursor
   ends up where it started.

The user sees a bright-red status banner in the UI while keep-awake is
active:

    ⚠ KEEP-AWAKE ACTIVE — system will not sleep | ETA: ~45 min | [STOP]

The banner is updated every 5 seconds with a fresh ETA. The user can
click STOP or the window close X at any time — both tear down the
keep-awake handles cleanly.

Design decisions:
- systemd-inhibit is the PRIMARY mechanism. Mouse nudging is secondary.
- Mouse nudging is OFF by default (opt-in via constructor flag) because
  it's visually intrusive. systemd-inhibit is always-on when available.
- The inhibit handle is held in a subprocess (not the main process) so
  it survives even if the GUI crashes — systemd cleans it up when the
  subprocess exits.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path


class KeepAwake:
    """Keep the system awake during a transcode.

    Usage::

        ka = KeepAwake(log_fn=worker.log_msg.emit)
        ka.start()
        try:
            # ... long encode ...
            while encoding:
                ka.update_eta(remaining_seconds)
                time.sleep(5)
        finally:
            ka.stop()  # releases inhibit + stops mouse nudging

    The ETA is displayed in the UI banner via ``update_eta()``.
    """

    def __init__(
        self,
        log_fn=None,
        enable_mouse_nudge: bool = False,
        nudge_interval: int = 60,
    ):
        self._log_fn = log_fn or (lambda msg: None)
        self._enable_mouse_nudge = enable_mouse_nudge and bool(shutil.which("xdotool"))
        self._nudge_interval = nudge_interval
        self._inhibit_proc: subprocess.Popen | None = None
        self._nudge_count = 0
        self._last_nudge = 0.0
        self._start_time = 0.0
        self._eta_seconds: float | None = None
        self._active = False

    def start(self) -> None:
        """Acquire systemd-inhibit handle. Safe to call multiple times."""
        if self._active:
            return
        self._active = True
        self._start_time = time.monotonic()
        self._acquire_inhibit()
        if self._enable_mouse_nudge:
            self._log_fn("KEEP-AWAKE: mouse nudging enabled (xdotool, every "
                         f"{self._nudge_interval}s)")
        else:
            self._log_fn("KEEP-AWAKE: mouse nudging disabled (xdotool not found "
                         "or not requested)")

    def stop(self) -> None:
        """Release the inhibit handle and stop nudging."""
        if not self._active:
            return
        self._active = False
        self._release_inhibit()
        if self._nudge_count > 0:
            self._log_fn(f"KEEP-AWAKE: stopped (mouse nudged {self._nudge_count} times)")

    def update_eta(self, remaining_seconds: float | None) -> None:
        """Update the ETA shown in the banner. None = unknown."""
        self._eta_seconds = remaining_seconds

    def tick(self) -> str | None:
        """Called periodically (e.g. every 5s) from the UI thread.

        Performs mouse nudge if interval has elapsed.
        Returns the current banner text, or None if keep-awake is not active.
        """
        if not self._active:
            return None
        now = time.monotonic()
        if self._enable_mouse_nudge and (now - self._last_nudge) >= self._nudge_interval:
            self._nudge_mouse()
            self._last_nudge = now
        return self.banner_text()

    def banner_text(self) -> str:
        """Return the bright-red banner text for the UI."""
        eta_str = self._format_eta(self._eta_seconds)
        elapsed = time.monotonic() - self._start_time
        elapsed_str = self._format_eta(elapsed)
        nudge_str = f" | mouse: {self._nudge_count}" if self._nudge_count > 0 else ""
        return (
            f"KEEP-AWAKE ACTIVE — system will not sleep | "
            f"elapsed: {elapsed_str} | ETA: {eta_str}{nudge_str}"
        )

    def _format_eta(self, seconds: float | None) -> str:
        if seconds is None:
            return "unknown"
        if seconds < 0:
            return "almost done"
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        if hours > 0:
            return f"~{hours}h{mins:02d}m"
        if mins > 0:
            return f"~{mins}m{secs:02d}s"
        return f"~{secs}s"

    def _acquire_inhibit(self) -> None:
        """Fork a systemd-inhibit subprocess that holds the sleep/hibernate
        inhibit handle for the duration of the transcode.

        systemd-inhibit takes a command to run while inhibiting. We pass
        ``sleep infinity`` (the GNU coreutils builtin) as the held command —
        it does nothing, runs forever, and the inhibit handle stays active
        until we kill the subprocess.
        """
        inhibit_bin = shutil.which("systemd-inhibit")
        if not inhibit_bin:
            self._log_fn("KEEP-AWAKE: systemd-inhibit not found — "
                         "system may sleep during transcode")
            return
        try:
            # --what=handle-lid-switch:sleep — inhibit both lid-close and
            #   automatic sleep/hibernate
            # --who=OpenTranscode — shown in `systemd-inhibit --list`
            # --why="Batch video transcode in progress" — shown in `systemd-inhibit --list`
            # --mode=block — block the action entirely (not just delay)
            self._inhibit_proc = subprocess.Popen(
                [
                    inhibit_bin,
                    "--what=sleep:idle",
                    "--who=OpenTranscode",
                    "--why=Batch video transcode in progress",
                    "--mode=block",
                    "sleep", "infinity",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # Don't put the child in a new session — we want it to die
                # when the parent dies (implicit via Popen + stop()).
            )
            self._log_fn("KEEP-AWAKE: systemd-inhibit active (sleep/idle blocked)")
        except (OSError, subprocess.SubprocessError) as e:
            self._log_fn(f"KEEP-AWAKE: failed to acquire systemd-inhibit: {e}")
            self._inhibit_proc = None

    def _release_inhibit(self) -> None:
        """Kill the systemd-inhibit subprocess to release the handle."""
        if self._inhibit_proc is None:
            return
        try:
            self._inhibit_proc.terminate()
            self._inhibit_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._inhibit_proc.kill()
            self._inhibit_proc.wait(timeout=1)
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            self._inhibit_proc = None
            self._log_fn("KEEP-AWAKE: systemd-inhibit released")

    def _nudge_mouse(self) -> None:
        """Move the mouse 1 pixel to prevent screen-blank.

        Uses xdotool. Alternates +1px right / -1px left so the cursor
        ends up where it started after every pair of nudges.
        """
        xdotool = shutil.which("xdotool")
        if not xdotool:
            return
        delta = 1 if (self._nudge_count % 2 == 0) else -1
        try:
            subprocess.run(
                [xdotool, "mousemove_relative", "--", str(delta), "0"],
                capture_output=True, timeout=3,
            )
            self._nudge_count += 1
        except (OSError, subprocess.SubprocessError):
            pass  # best-effort — don't crash the transcode over a nudge

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def has_inhibit(self) -> bool:
        return self._inhibit_proc is not None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()
