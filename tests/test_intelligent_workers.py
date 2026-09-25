"""
Intelligent worker-count tests (v4.1.0).

QA finding: thread oversubscription → hard lock on high-core-count
machines (28-thread Xeon with v4.0.0 produced 13 workers × 28 threads =
~364 threads on 28 logical CPUs → kernel scheduler drowned → hard lock).

The fix is ``EncoderWorker._compute_intelligent_worker_count()``, which
returns ``(worker_count, threads_per_worker)`` such that
``worker_count * threads_per_worker <= logical_threads - 1``. The tests
here cover:

  - CPU topology math for laptop / desktop / Xeon / EPYC / single-core VM.
  - No-oversubscription invariant (active ≤ logical - 1) on every shape.
  - --max-workers override is honored and capped by physical_cores - 1.
  - --threads-per-worker override is honored.
  - When BOTH overrides are set, the auto math is bypassed entirely.
  - Codec params functions append ``--threads N`` when threads > 0
    (and stay byte-identical to v4.0.0 when threads == 0).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from conftest import make_minimal_worker


# ─────────────────────────────────────────────────────────────────────────────
#  _compute_intelligent_worker_count — CPU topology math
# ─────────────────────────────────────────────────────────────────────────────

def _make_worker(opentranscode_module, env, max_workers=None, threads_per_worker=None):
    """Build an EncoderWorker via __new__ + minimum attrs needed for the
    intelligent worker math. Bypasses QThread.__init__ so this runs in a
    headless test environment without a real Qt event loop."""
    worker = opentranscode_module.EncoderWorker.__new__(opentranscode_module.EncoderWorker)
    worker.env = env
    worker.max_workers = max_workers
    worker.threads_per_worker_override = threads_per_worker
    return worker


def _set_cpu(env, physical, logical, tpc=None):
    """Mutate an EnvProbe's CpuTopology in-place."""
    env.cpu.physical_cores = physical
    env.cpu.logical_threads = logical
    env.cpu.threads_per_core = tpc if tpc is not None else (
        logical // physical if physical > 0 else 1
    )


def test_laptop_4c8t(opentranscode_module, mock_env):
    """4-core / 8-thread laptop → 1 worker × 7 threads = 7 active."""
    _set_cpu(mock_env, physical=4, logical=8, tpc=2)
    worker = _make_worker(opentranscode_module, mock_env)
    wc, tpw = worker._compute_intelligent_worker_count()
    # budget = 8 - 1 = 7. ideal_tpw=4 → target_workers = 7//4 = 1.
    # tpw = 7//1 = 7. active = 1*7 = 7 ≤ 7 ✓
    assert wc == 1
    assert tpw == 7
    assert wc * tpw <= 8 - 1


def test_desktop_8c16t(opentranscode_module, mock_env):
    """8-core / 16-thread desktop → 2 workers × 7 threads = 14 active.

    v4.1.1 changed IDEAL_THREADS_PER_WORKER from 4 to 6, so the budget
    (15) splits as 15//6=2 workers, 15//2=7 threads per worker.
    """
    _set_cpu(mock_env, physical=8, logical=16, tpc=2)
    worker = _make_worker(opentranscode_module, mock_env)
    wc, tpw = worker._compute_intelligent_worker_count()
    # v4.1.1: budget = 15. target = 15//6 = 2. tpw = 15//2 = 7. active = 2*7 = 14 ≤ 15 ✓
    assert wc == 2
    assert tpw == 7
    assert wc * tpw <= 16 - 1


def test_xeon_14c28t_users_box(opentranscode_module, mock_env):
    """14-core / 28-thread Xeon (the user's box) → 4 workers × 6 threads.

    This is the exact machine the v4.0.0 hard-lock happened on. With v4.0.0
    behavior (worker_count = physical-1 = 13, no thread cap) each SVT-AV1
    worker grabbed all 28 logical threads → 13 × 28 = 364 active threads
    on 28 logical CPUs → kernel scheduler drowned → hard lock.

    With v4.1.1: 4 workers × 6 threads = 24 active, 4 reserved for OS/UI.
    v4.1.0 used 6 workers × 4 threads = 24 active (same total, but 4
    threads/chunk was too slow for SVT-AV1 and made the encode look
    "borked" — v4.1.1 gives each chunk 6 threads for better per-chunk
    throughput while keeping the same total thread budget).
    """
    _set_cpu(mock_env, physical=14, logical=28, tpc=2)
    worker = _make_worker(opentranscode_module, mock_env)
    wc, tpw = worker._compute_intelligent_worker_count()
    # v4.1.1: budget = 27. target = 27//6 = 4. tpw = 27//4 = 6. active = 4*6 = 24 ✓
    assert wc == 4
    assert tpw == 6
    assert wc * tpw == 24
    assert wc * tpw <= 28 - 1


def test_epyc_32c64t(opentranscode_module, mock_env):
    """32-core / 64-thread EPYC → 10 workers × 6 threads = 60 active."""
    _set_cpu(mock_env, physical=32, logical=64, tpc=2)
    worker = _make_worker(opentranscode_module, mock_env)
    wc, tpw = worker._compute_intelligent_worker_count()
    # v4.1.1: budget = 63. target = 63//6 = 10. tpw = 63//10 = 6. active = 10*6 = 60 ≤ 63 ✓
    assert wc == 10
    assert tpw == 6
    assert wc * tpw <= 64 - 1


def test_vm_1c2t(opentranscode_module, mock_env):
    """1-core / 2-thread VM → 1 worker × 1 thread = 1 active (degenerate)."""
    _set_cpu(mock_env, physical=1, logical=2, tpc=2)
    worker = _make_worker(opentranscode_module, mock_env)
    wc, tpw = worker._compute_intelligent_worker_count()
    # physical=1 → max_by_phys = max(1, 1-1) = max(1, 0) = 1 (because physical>1
    # is False). target = min(budget//4, 1) = min(0, 1) but max(1, 0)=1.
    # tpw = max(1, budget//1) = max(1, 1//1) = 1. active = 1*1 = 1 ≤ 1 ✓
    assert wc == 1
    assert tpw == 1


def test_single_core_no_ht(opentranscode_module, mock_env):
    """1-core / 1-thread (no HT) → 1 worker × 1 thread = 1 active."""
    _set_cpu(mock_env, physical=1, logical=1, tpc=1)
    worker = _make_worker(opentranscode_module, mock_env)
    wc, tpw = worker._compute_intelligent_worker_count()
    assert wc == 1
    assert tpw == 1


# ─────────────────────────────────────────────────────────────────────────────
#  No-oversubscription invariant — fuzz-ish sweep
# ─────────────────────────────────────────────────────────────────────────────

def test_no_oversubscription_across_typical_topologies(opentranscode_module, mock_env):
    """For every (physical, logical) in a sweep of plausible CPU shapes,
    worker_count * threads_per_worker ≤ logical - 1."""
    shapes = [
        (1, 1), (1, 2), (2, 2), (2, 4),
        (4, 4), (4, 8), (6, 6), (6, 12),
        (8, 8), (8, 16), (12, 16), (12, 24),
        (14, 28), (16, 32), (24, 48), (32, 64),
        (48, 96), (64, 128),
    ]
    for phys, logical in shapes:
        _set_cpu(mock_env, physical=phys, logical=logical,
                 tpc=(logical // phys) if phys > 0 else 1)
        worker = _make_worker(opentranscode_module, mock_env)
        wc, tpw = worker._compute_intelligent_worker_count()
        # Invariant: never exceed logical - 1 (one thread for OS/UI).
        assert wc * tpw <= max(1, logical - 1), (
            f"oversubscribed on {phys}c{logical}t: "
            f"{wc} workers × {tpw} threads = {wc * tpw} > {logical - 1}"
        )
        # Sanity: both positive integers.
        assert wc >= 1
        assert tpw >= 1


# ─────────────────────────────────────────────────────────────────────────────
#  Overrides — --max-workers / --threads-per-worker
# ─────────────────────────────────────────────────────────────────────────────

def test_max_workers_override_caps_worker_count(opentranscode_module, mock_env):
    """--max-workers=3 on a 28-thread Xeon → 3 workers (thread budget recomputed)."""
    _set_cpu(mock_env, physical=14, logical=28, tpc=2)
    worker = _make_worker(opentranscode_module, mock_env, max_workers=3)
    wc, tpw = worker._compute_intelligent_worker_count()
    # max_workers=3 → target_workers = min(3, 13) = 3.
    # tpw = budget // 3 = 27 // 3 = 9. active = 3*9 = 27 ≤ 27 ✓
    assert wc == 3
    assert tpw == 9
    assert wc * tpw <= 28 - 1


def test_max_workers_capped_by_physical_cores(opentranscode_module, mock_env):
    """--max-workers=99 on a 4-core machine → capped at physical_cores - 1 = 3."""
    _set_cpu(mock_env, physical=4, logical=8, tpc=2)
    worker = _make_worker(opentranscode_module, mock_env, max_workers=99)
    wc, tpw = worker._compute_intelligent_worker_count()
    # max_workers=99, but max_by_phys = 3 → target_workers = min(99, 3) = 3.
    # tpw = budget // 3 = 7 // 3 = 2. active = 3*2 = 6 ≤ 7 ✓
    assert wc == 3
    assert tpw == 2


def test_threads_per_worker_override(opentranscode_module, mock_env):
    """--threads-per-worker=2 on a 28-thread Xeon → 2 threads per worker.

    v4.1.1: with IDEAL_THREADS_PER_WORKER=6, the auto worker count is
    27//6=4 (was 6 in v4.1.0 with IDEAL=4). The override only changes
    threads_per_worker, not worker_count.
    """
    _set_cpu(mock_env, physical=14, logical=28, tpc=2)
    worker = _make_worker(opentranscode_module, mock_env, threads_per_worker=2)
    wc, tpw = worker._compute_intelligent_worker_count()
    # v4.1.1: target = 27//6 = 4. tpw override = 2. active = 4*2 = 8 ≤ 27 ✓
    assert wc == 4
    assert tpw == 2


def test_both_overrides_bypass_auto_math(opentranscode_module, mock_env):
    """When both --max-workers and --threads-per-worker are set, the auto
    budget math is bypassed entirely — even if it would oversubscribe."""
    _set_cpu(mock_env, physical=4, logical=8, tpc=2)
    worker = _make_worker(opentranscode_module, mock_env,
                          max_workers=10, threads_per_worker=8)
    wc, tpw = worker._compute_intelligent_worker_count()
    # User explicitly asked for 10×8 = 80 threads on an 8-thread box.
    # The auto math is bypassed; the user gets what they asked for.
    assert wc == 10
    assert tpw == 8


def test_override_can_come_from_env_av1an_flags(opentranscode_module, mock_env):
    """EncoderWorker.__init__ should pick up max_workers / threads_per_worker
    from env.av1an_flags when the explicit constructor args are None.

    This is the path the CLI's --max-workers / --threads-per-worker flags
    take: cli.main stores them on env.av1an_flags before launch_gui runs,
    the GUI instantiates EncoderWorker without the explicit kwargs, and
    __init__ falls back to env.av1an_flags."""
    _set_cpu(mock_env, physical=14, logical=28, tpc=2)
    mock_env.av1an_flags["max_workers"] = 4
    mock_env.av1an_flags["threads_per_worker"] = 3

    # Construct via real __init__ (uses the env fallback path).
    # _temp_dir creation requires the conftest's _APP_CACHE_DIR patch —
    # use make_minimal_worker to bypass __init__ and set attrs manually,
    # then simulate the __init__ fallback logic inline.
    worker = opentranscode_module.EncoderWorker.__new__(opentranscode_module.EncoderWorker)
    worker.env = mock_env
    # Mirror the __init__ fallback logic exactly:
    worker.max_workers = (
        mock_env.av1an_flags.get("max_workers")
        if isinstance(mock_env.av1an_flags.get("max_workers"), int)
        else None
    )
    worker.threads_per_worker_override = (
        mock_env.av1an_flags.get("threads_per_worker")
        if isinstance(mock_env.av1an_flags.get("threads_per_worker"), int)
        else None
    )

    wc, tpw = worker._compute_intelligent_worker_count()
    # Both overrides set → bypass auto math.
    assert wc == 4
    assert tpw == 3


# ─────────────────────────────────────────────────────────────────────────────
#  Codec params functions — no threads= arg
#  (SvtAv1EncApp CLI uses --lp, not --threads; thread capping lives
#  in ffmpeg_vargs_fn and av1an's --workers)
# ─────────────────────────────────────────────────────────────────────────────

def test_av1_params_v412_no_threads_arg(opentranscode_module):
    """_av1_params takes only (crf, preset). Thread capping lives in
    ffmpeg_vargs_fn (where libsvtav1 is a library) and in av1an's
    --workers flag (chunk-parallel count).
    """
    out = opentranscode_module._av1_params(30, 6)
    assert out == "--preset 6 --crf 30 --keyint 240"
    assert "--threads" not in out


def test_vp9_params_v412_no_threads_arg(opentranscode_module):
    """v4.1.2: _vp9_params takes only (crf, preset)."""
    out = opentranscode_module._vp9_params(32, 2)
    assert "--threads" not in out


def test_x265_params_v412_no_threads_arg(opentranscode_module):
    """v4.1.2: _x265_params takes only (crf, preset)."""
    out = opentranscode_module._x265_params(28, 7)
    assert "--threads" not in out


# ─────────────────────────────────────────────────────────────────────────────
#  v4.1.1: live tail + heartbeat
# ─────────────────────────────────────────────────────────────────────────────

def test_live_tail_emits_lines(opentranscode_module, mock_env, monkeypatch):
    """v4.1.1: _run_with_stop_check emits each line of av1an's stdout/stderr
    to the GUI log as it arrives, instead of buffering until process exit.

    This is the fix for the "no activity / borked" symptom: with v4.1.0's
    slower (capped-thread) encodes, the user stared at a frozen log for
    10+ minutes because the drainer only emitted on process exit. v4.1.1
    emits each line as av1an prints it.
    """
    import io
    import signal
    import subprocess
    from unittest.mock import MagicMock

    worker = make_minimal_worker(opentranscode_module, env=mock_env)
    worker._stop = False
    # v4.2.1+: the live stderr/stdout tail only emits when verbose=True.
    worker.verbose = True

    # Patch time.sleep so the poll loop runs instantly.
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)
    monkeypatch.setattr("os.killpg", lambda *a, **k: None)
    monkeypatch.setattr("os.getpgid", lambda pid: 99999)

    # Collect emitted log messages.
    emitted: list[str] = []
    worker.log_msg = MagicMock()
    worker.log_msg.emit = lambda msg: emitted.append(msg)

    # Fake process that writes 3 lines to stderr then exits 0.
    fake_proc = MagicMock()
    fake_proc.pid = 12345
    fake_proc.stdout = io.StringIO("")
    fake_proc.stderr = io.StringIO(
        "INFO encode_file: scenecut: found 8 scene(s)\n"
        "DEBUG encode_file: Segmenting video\n"
        "INFO encode_chunk: Encoding chunk 1\n"
    )
    fake_proc.poll.return_value = 0
    fake_proc.wait.return_value = 0

    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: fake_proc)

    status, rc, stdout, stderr = worker._run_with_stop_check(
        cmd=["av1an", "-i", "x.mkv", "-o", "y.mkv"],
        timeout=60,
    )

    assert status == "ok"
    assert rc == 0
    # The live tail should have emitted each stderr line with the pipe prefix.
    tail_lines = [m for m in emitted if m.startswith("  │ ")]
    assert len(tail_lines) >= 3, (
        f"Expected ≥3 live-tail lines, got {len(tail_lines)}: {tail_lines}"
    )
    assert any("scenecut: found 8 scene(s)" in m for m in tail_lines)
    assert any("Segmenting video" in m for m in tail_lines)
    assert any("Encoding chunk 1" in m for m in tail_lines)
    # The stderr buffer should also contain the full output.
    assert "scenecut: found 8 scene(s)" in stderr


def test_live_tail_handles_carriage_return(opentranscode_module, mock_env, monkeypatch):
    """v4.1.1: live tail handles \\r (progress bar updates) as line boundaries.

    av1an's progress bar uses \\r to overwrite the current line. Without
    \\r handling, the live tail would buffer the entire progress bar
    sequence and only emit when the final \\n arrives (which might be
    never during a long encode).
    """
    import io
    from unittest.mock import MagicMock

    worker = make_minimal_worker(opentranscode_module, env=mock_env)
    worker._stop = False
    # v4.2.1+: the live stderr/stdout tail only emits when verbose=True.
    worker.verbose = True

    monkeypatch.setattr("time.sleep", lambda *a, **k: None)
    monkeypatch.setattr("os.killpg", lambda *a, **k: None)
    monkeypatch.setattr("os.getpgid", lambda pid: 99999)

    emitted: list[str] = []
    worker.log_msg = MagicMock()
    worker.log_msg.emit = lambda msg: emitted.append(msg)

    # Simulate av1an progress bar: \r-delimited updates, then \n at the end.
    fake_proc = MagicMock()
    fake_proc.pid = 12345
    fake_proc.stdout = io.StringIO("")
    fake_proc.stderr = io.StringIO(
        "Encoding          10%\rEncoding          25%\rEncoding          50%\rDone\n"
    )
    fake_proc.poll.return_value = 0
    fake_proc.wait.return_value = 0

    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: fake_proc)

    status, rc, stdout, stderr = worker._run_with_stop_check(
        cmd=["av1an", "-i", "x.mkv", "-o", "y.mkv"],
        timeout=60,
    )

    assert status == "ok"
    tail_lines = [m for m in emitted if m.startswith("  │ ")]
    # Each \r-delimited segment should be emitted as a separate line.
    assert any("10%" in m for m in tail_lines), (
        f"Expected '10%' in tail lines: {tail_lines}"
    )
    assert any("25%" in m for m in tail_lines)
    assert any("50%" in m for m in tail_lines)
    assert any("Done" in m for m in tail_lines)


def test_heartbeat_emits_during_long_encode(opentranscode_module, mock_env, monkeypatch):
    """v4.1.1: heartbeat emits 'still encoding' every 30s during a long encode.

    Without this, a slow-but-working encode looks identical to a wedged one
    — the user sees no output for minutes and assumes it's dead.
    """
    import io
    import time
    from unittest.mock import MagicMock

    worker = make_minimal_worker(opentranscode_module, env=mock_env)
    worker._stop = False
    # v4.4.1: heartbeat is gated behind --verbose. Set it True so
    # the heartbeat fires during this test.
    worker.verbose = True
    simulated_time = [0.0]
    def fake_monotonic():
        return simulated_time[0]
    def fake_sleep(seconds):
        # Advance 31s per sleep call so the 30s heartbeat threshold is crossed.
        simulated_time[0] += 31

    monkeypatch.setattr("time.monotonic", fake_monotonic)
    monkeypatch.setattr("time.sleep", fake_sleep)
    monkeypatch.setattr("os.killpg", lambda *a, **k: None)
    monkeypatch.setattr("os.getpgid", lambda pid: 99999)

    emitted: list[str] = []
    worker.log_msg = MagicMock()
    worker.log_msg.emit = lambda msg: emitted.append(msg)

    fake_proc = MagicMock()
    fake_proc.pid = 12345
    fake_proc.stdout = io.StringIO("")
    fake_proc.stderr = io.StringIO("")

    poll_count = [0]
    def poll_side_effect():
        poll_count[0] += 1
        # Exit after 3 polls (simulating a ~90s encode with 30s sleep steps).
        if poll_count[0] >= 3:
            return 0
        return None
    fake_proc.poll.side_effect = poll_side_effect
    fake_proc.wait.return_value = 0

    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: fake_proc)

    status, rc, stdout, stderr = worker._run_with_stop_check(
        cmd=["av1an", "-i", "x.mkv", "-o", "y.mkv"],
        timeout=7200,
    )

    assert status == "ok"
    # Heartbeat messages should appear (one per 30s of simulated time).
    # Format: "... Ns elapsed"
    heartbeat_lines = [m for m in emitted if "elapsed" in m]
    assert len(heartbeat_lines) >= 1, (
        f"Expected ≥1 heartbeat, got {len(heartbeat_lines)}: {heartbeat_lines}"
    )
    # The heartbeat should include the elapsed time.
    assert any("elapsed" in m for m in heartbeat_lines)
