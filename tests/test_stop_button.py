"""
STOP-button tests for ``EncoderWorker._run_with_stop_check``.

QA finding: OTC-013 (concurrent-worker safety + responsive STOP).

``_run_with_stop_check`` is the open-transcode.py replacement for the v2 ``subprocess.run``
calls inside the av1an and ffmpeg-fallback encode paths. It:

  - Spawns the subprocess via ``Popen(start_new_session=True)`` so it can be
    signaled as a *process group* (reaches av1an's child encoders —
    SvtAv1EncApp / vpxenc / x265 — not just the av1an parent).
  - Polls ``self._stop`` every ~1 second.
  - On STOP: SIGTERM the process group, wait 5s, SIGKILL if still alive.
    Returns ``("stop", rc, stdout, stderr)``.
  - On normal exit: returns ``("ok", rc, stdout, stderr)``.
  - On overall timeout: SIGKILL the group. Returns ``("timeout", ...)``.

The 2 cases:
  - STOP requested mid-encode -> status="stop", SIGTERM + SIGKILL sent
    via os.killpg.
  - Happy path -> status="ok", rc=0, no signals sent.

Both cases mock ``subprocess.Popen`` (via the ``mock_subprocess_popen``
fixture or directly) and ``time.sleep`` (so the 1-second poll loop runs
instantly). ``os.killpg`` and ``os.getpgid`` are also patched so no real
process-group signaling happens.
"""

from __future__ import annotations

import io
import signal
import subprocess
import time
from unittest.mock import MagicMock

import pytest

from conftest import make_minimal_worker


def test_stop_terminates_subprocess(opentranscode_module, monkeypatch):
    """STOP mid-encode -> status="stop", SIGTERM then SIGKILL sent to group.

    The poll loop runs:
      - Iteration 1: poll() -> None, _stop=False, sleep(1) [patched to no-op]
      - Iteration 2: poll() -> None, _stop=False, sleep(1) [patched to no-op]
      - Iteration 3: poll() -> None; side-effect sets _stop=True;
        stop branch fires: SIGTERM via os.killpg, proc.wait(5) raises
        TimeoutExpired (simulating av1an not responding to SIGTERM within
        the grace period), SIGKILL via os.killpg, proc.wait(2) returns
        None. status="stop", break.
    """
    worker = make_minimal_worker(opentranscode_module)
    worker._stop = False

    # Patch time.sleep so the 1-second poll loop runs instantly.
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)

    # Track os.killpg calls: (pgid, signal) tuples.
    killpg_calls: list[tuple[int, int]] = []

    def fake_killpg(pgid, sig):
        killpg_calls.append((pgid, sig))

    monkeypatch.setattr("os.killpg", fake_killpg)
    monkeypatch.setattr("os.getpgid", lambda pid: 99999)  # fake PGID

    # Build a fake Popen result. stdout/stderr are StringIO("") so the
    # open-transcode module's drainer threads (which call .read(4096)) hit EOF
    # immediately and exit cleanly.
    fake_proc = MagicMock()
    fake_proc.pid = 12345
    fake_proc.stdout = io.StringIO("")
    fake_proc.stderr = io.StringIO("")

    poll_calls = [0]

    def poll_side_effect():
        poll_calls[0] += 1
        # After 2 polls (i.e. on the 3rd), request STOP. This simulates
        # the user clicking the STOP button while the encode is running.
        if poll_calls[0] == 3:
            worker._stop = True
        # Always return None — the process never exits on its own; the
        # STOP branch handles termination.
        return None

    fake_proc.poll.side_effect = poll_side_effect

    # First proc.wait (after SIGTERM) raises TimeoutExpired -> triggers
    # the SIGKILL escalation branch. Second proc.wait (after SIGKILL)
    # returns None (process reaped).
    fake_proc.wait.side_effect = [
        subprocess.TimeoutExpired(cmd=["test"], timeout=5),
        None,
    ]

    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: fake_proc)

    status, rc, stdout, stderr = worker._run_with_stop_check(
        cmd=["av1an", "-i", "x.mkv", "-o", "y.mkv"],
        timeout=60,
        log_prefix="  ",
    )

    assert status == "stop", (
        f"Expected status='stop' when STOP requested, got {status!r}"
    )
    # SIGTERM must be sent first (graceful), then SIGKILL after the 5s
    # grace period expires (simulated by the wait() TimeoutExpired).
    assert (99999, signal.SIGTERM) in killpg_calls, (
        f"SIGTERM not sent to process group. killpg calls: {killpg_calls}"
    )
    assert (99999, signal.SIGKILL) in killpg_calls, (
        f"SIGKILL not sent after wait() timed out. killpg calls: {killpg_calls}"
    )
    # SIGTERM should come before SIGKILL (graceful before forceful).
    sigterm_idx = killpg_calls.index((99999, signal.SIGTERM))
    sigkill_idx = killpg_calls.index((99999, signal.SIGKILL))
    assert sigterm_idx < sigkill_idx, (
        f"SIGTERM must be sent before SIGKILL. calls: {killpg_calls}"
    )


def test_happy_path_completes_normally(opentranscode_module, monkeypatch):
    """Encode exits normally -> status="ok", rc=0, no signals sent.

    poll() returns 0 immediately (process exited cleanly). No STOP, no
    timeout, no os.killpg calls.
    """
    worker = make_minimal_worker(opentranscode_module)
    worker._stop = False

    monkeypatch.setattr("time.sleep", lambda *a, **k: None)

    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr("os.killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))
    monkeypatch.setattr("os.getpgid", lambda pid: 99999)

    fake_proc = MagicMock()
    fake_proc.pid = 12345
    fake_proc.stdout = io.StringIO("av1an progress line\n")
    fake_proc.stderr = io.StringIO("")
    # First poll returns 0 (process exited cleanly with success).
    fake_proc.poll.return_value = 0
    fake_proc.wait.return_value = 0

    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: fake_proc)

    status, rc, stdout, stderr = worker._run_with_stop_check(
        cmd=["av1an", "-i", "x.mkv", "-o", "y.mkv"],
        timeout=60,
    )

    assert status == "ok"
    assert rc == 0
    assert killpg_calls == [], (
        f"No signals should be sent on happy path. killpg calls: {killpg_calls}"
    )
    # Drainer threads should have captured the stdout content.
    assert "av1an progress line" in stdout
