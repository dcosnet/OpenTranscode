"""opentranscode — open-source batch video transcoder (av1an + ffmpeg).

A PySide6 GUI application that orchestrates av1an + ffmpeg for batch video
transcoding. Distro-aware, config-driven (codec / audio / container /
resolution / license profiles), with a QThread-based encoder worker, a
from-git source builder for resolving VapourSynth / av1an ABI mismatches,
and a retro-futuristic media-console UI.

This package is the production code path. The launcher script
``open-transcode.py`` is preserved alongside it for backwards
compatibility and as the test target for the mocked test suite.

Run as a module:
    python -m opentranscode            # launch the GUI
    python -m opentranscode --version  # print version and exit
    python -m opentranscode --dry-run  # probe env + smoke test, no GUI
    python -m opentranscode --verify-only /path/to/output.mkv

Or import in code:
    import opentranscode
    print(opentranscode.__version__)
"""

from __future__ import annotations

__version__ = "4.8.1"
__author__ = "Jeremy Anderson - dcos.net"
__license__ = "AGPL-3.0"

__all__ = [
    "__version__",
    "__author__",
    "__license__",
    "build_parser",
    "main",
    "launch_gui",
]

# Lightweight re-exports for convenience. Heavy modules (env_probe,
# encoder_worker, ui_window) are NOT imported here so that
# ``import opentranscode`` works without PySide6 being available — this
# keeps ``opentranscode.__version__`` cheap and side-effect-free for
# ``--version`` and for tooling that just wants the metadata.
from .cli import build_parser, main


def launch_gui(argv: list[str] | None = None, force: bool = False,
               chunk_method: str | None = None,
               max_workers: int | None = None,
               threads_per_worker: int | None = None,
               use_av1an: bool = False,
               verbose: bool = False,
               skip_existing: bool = True,
               timeout: int = 86400,
               inline_scale: bool = False,
               engine: str = "auto",
               gpu_profile: str = "auto") -> int:
    """Launch the OpenTranscode GUI.

    Thin wrapper around ``opentranscode.ui_window.launch_gui``; imported
    lazily so that ``import opentranscode`` does not pull in PySide6.

    Args:
        argv: Optional argv list for QApplication. Defaults to sys.argv.
        force: Pre-check the "Force (skip validation)" checkbox — skips
            ffprobe pre-validation and attempts encode even for files
            ffprobe cannot read. WARNING: invalid files will waste the
            full per-file timeout before failing.
        chunk_method: Override av1an's chunk-method selection (v4.0.0).
            When not None, the value is written to
            ``env.av1an_flags["chunk_method_override"]`` after the
            environment probe runs, so every EncoderWorker picks it up.
            Useful for forcing ``select`` to avoid the Hybrid chunk
            method's failure on phone-recorded MP4s with sparse
            keyframes. ``"auto"`` clears any override the probe set.
        max_workers: Override the chunk-parallel worker count (v4.1.0).
            When None, EncoderWorker computes from CPU topology so that
            ``worker_count * threads_per_worker <= logical_threads - 1``.
            Stored on ``env.av1an_flags["max_workers"]`` so the
            GUI-spawned worker picks it up.
        threads_per_worker: Override the per-encoder thread cap (v4.1.0).
            When None, computed as ``max(1, budget // worker_count)``.
            Stored on ``env.av1an_flags["threads_per_worker"]`` so the
            GUI-spawned worker picks it up.
        use_av1an: Opt into av1an chunk-parallel encoding (v4.2.0).
            Default False = ffmpeg-only (more reliable across distros).
            When True, the av1an pre-flight + smoke test runs as before.
            av1an was too fragile: y4m pipe breaks, SvtAv1EncApp CLI
            rejects --threads, VapourSynth plugin issues, output
            buffering making it look hung. ffmpeg's libsvtav1 is invoked
            as a library, doesn't need VapourSynth, and produces
            immediate progress output.
        verbose: Enable verbose log output (v4.2.1). Default False =
            quiet (per-file success/fail + final summary only). True
            = full tech detail (CMD: lines, live tail of av1an/ffmpeg
            stderr, DIAGNOSIS blocks, resolution map, pre-flight
            validation table, 30s heartbeat).
        inline_scale: Skip the CRF-16 pre-scale intermediate when a
            target resolution is selected (v4.4.4). The scale/pad filter
            chain is passed directly to av1an via --ffmpeg-filter-args
            instead. Eliminates the 0.5-0.8x source size intermediate
            that was crashing 10GB+ encodes with mysterious "ffmpeg
            error (rc=234)" disk-exhaustion messages. Default False —
            the intermediate path is more robust on older av1an/
            VapourSynth builds. Pre-checks the "Inline scale" UI checkbox.
        engine: Video encode engine (v4.6.0). "auto" (default) uses the
            NVENC GPU encoder when the selected codec family has one and
            the live encode test proved it works; "gpu" forces NVENC;
            "cpu" forces the software encoders; "hybrid" (v4.7.0) splits
            the queue between a GPU lane and a CPU lane running
            concurrently. Pre-selects the ENGINE combo in the UI.
    """
    from .ui_window import launch_gui as _launch
    return _launch(
        argv, force=force, chunk_method=chunk_method,
        max_workers=max_workers, threads_per_worker=threads_per_worker,
        use_av1an=use_av1an, verbose=verbose, skip_existing=skip_existing,
        timeout=timeout, inline_scale=inline_scale, engine=engine,
        gpu_profile=gpu_profile,
    )
