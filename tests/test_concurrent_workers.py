"""
Concurrent-worker temp-dir isolation test.

QA finding: OTC-013 (per-worker temp dir isolation).

Each ``EncoderWorker`` is assigned its own per-PID subdirectory under the
shared app temp dir (``_worker_temp_dir(os.getpid())`` in open-transcode.py). This is
critical for concurrency: the final cleanup sweep (``_final_cleanup_sweep``)
deletes everything inside ``self._temp_dir`` and must NOT touch a sibling
worker's intermediates.

The 1 case:
  - Two workers (with distinct PIDs) get distinct ``_temp_dir`` paths.

The test patches ``os.getpid`` to return distinct values for the two
``EncoderWorker()`` constructor calls (since both run in the same test
process and would otherwise share a PID), and redirects the shared app
temp dir to ``tmp_path`` so the real ``~/.cache/OpenTranscode/`` is not
touched.
"""

from __future__ import annotations

import pytest


def test_workers_get_distinct_temp_dirs(opentranscode_module, mock_env, tmp_path, monkeypatch):
    """Two workers with different PIDs get different ``_temp_dir`` paths.

    The directory naming convention is ``worker-{pid}`` under the shared
    app temp dir. Distinct PIDs => distinct subdir names => no overlap,
    so each worker's cleanup sweep is isolated from concurrent workers.
    """
    # Redirect the shared app temp dir to tmp_path so the real
    # ~/.cache/OpenTranscode/ is NOT touched by this test.
    opentranscode_module._APP_CACHE_DIR = tmp_path

    # Two distinct fake PIDs for the two workers. (In production, workers
    # run in separate OS processes via the distro's av1an binary, which
    # itself spawns SvtAv1EncApp / vpxenc / x265 as subprocesses — each
    # getting its own PID. Even within a single process, the per-PID
    # subdir logic ensures concurrent workers don't collide on temp space.)
    pids = iter([11111, 22222])
    monkeypatch.setattr("os.getpid", lambda: next(pids))

    # Build two real EncoderWorker instances via __init__. __init__ does
    # NOT start the QThread (only .start() does), so this is safe in a
    # headless test environment.
    common_kwargs = dict(
        in_dir=tmp_path / "in",
        out_dir=tmp_path / "out",
        video_codec=opentranscode_module.VIDEO_CODECS[0],
        audio_profile=opentranscode_module.AUDIO_PROFILES[0],
        container=opentranscode_module.CONTAINER_PROFILES[0],
        crf=30,
        preset_label="Medium (6)",
        delete_source=False,
        env=mock_env,
        extensions={".mkv"},
        resolution=opentranscode_module.RESOLUTION_PRESETS[0],  # "Original" (no scaling)
    )
    worker1 = opentranscode_module.EncoderWorker(**common_kwargs)
    worker2 = opentranscode_module.EncoderWorker(**common_kwargs)

    # The critical assertion: distinct temp dirs.
    assert worker1._temp_dir != worker2._temp_dir, (
        f"Two concurrent workers got the same _temp_dir: {worker1._temp_dir}"
    )
    # Both should be subdirs of the shared app temp dir, with the per-PID
    # naming convention.
    assert worker1._temp_dir.parent == tmp_path
    assert worker2._temp_dir.parent == tmp_path
    assert worker1._temp_dir.name == "worker-11111"
    assert worker2._temp_dir.name == "worker-22222"
    # Both subdirs should actually exist on disk (the constructor creates
    # them with mode=0o700 per SEI CERT FIO09-C).
    assert worker1._temp_dir.exists()
    assert worker2._temp_dir.exists()
