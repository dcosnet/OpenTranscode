"""Command-line interface for opentranscode.

Provides three flags:
  - ``--version``      — print the package version and exit (0).
  - ``--dry-run``      — probe the environment, run the av1an VSScript
    smoke test if av1an is available, print a report, and exit. Does
    NOT launch the GUI and does NOT encode anything.
  - ``--verify-only PATH`` — re-verify an existing output file's size,
    resolution, and duration via ffprobe, without re-encoding.

With no flag, ``main()`` defers to ``ui_window.launch_gui()``.

Heavy imports (``env_probe``, ``ffprobe_utils``, ``ui_window``) are
deferred into the bodies of ``run_dry_run`` / ``run_verify_only`` /
the no-flag branch so that ``--version`` does not pull in PySide6.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="opentranscode",
        description="Open-source batch video transcoder (av1an + ffmpeg)",
    )
    parser.add_argument(
        "--version", action="store_true",
        help="Print version and exit",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Probe environment, run smoke test, print report — but do "
             "NOT launch GUI or encode anything",
    )
    parser.add_argument(
        "--verify-only", metavar="PATH",
        help="Re-verify an existing output file (size, resolution, "
             "duration checks) without re-encoding",
    )
    # v5-01: --force pre-checks the "Force (skip validation)" checkbox in
    # the GUI. This is a convenience flag — the checkbox can also be toggled
    # manually in the UI.
    parser.add_argument(
        "--force", action="store_true",
        help="Pre-check the 'Force (skip validation)' checkbox in the GUI. "
             "Skips ffprobe pre-validation and attempts encode even for "
             "files ffprobe cannot read. WARNING: invalid files will waste "
             "the full per-file timeout before failing.",
    )
    # v4.0.0: --chunk-method overrides av1an's chunk-method selection. Useful
    # for debugging the "works up until near the end, never saves chunks
    # into a full file" bug (Hybrid chunk method on phone-recorded MP4s).
    # When set, the value is written to env.av1an_flags["chunk_method_override"]
    # before the GUI launches, so every EncoderWorker picks it up.
    parser.add_argument(
        "--chunk-method", metavar="METHOD",
        choices=["auto", "select", "hybrid", "segment", "ffms2",
                 "lsmash", "bestsource", "dgdecnv"],
        help="Force av1an to use a specific chunk method. 'select' is the "
             "most reliable (uses VapourSynth's select() filter) but slowest. "
             "'hybrid' (av1an's default when no VS plugins) fails on phone-"
             "recorded MP4s with sparse keyframes. 'ffms2'/'lsmash'/"
             "'bestsource' require the corresponding VapourSynth plugin. "
             "'auto' lets av1an decide (default).",
    )
    # v4.1.0: intelligent chunking overrides. When neither flag is given,
    # EncoderWorker computes (worker_count, threads_per_worker) from CPU
    # topology so worker_count * threads_per_worker <= logical_threads - 1.
    # This prevents the thread-oversubscription hard-lock that v4.0.0 hit
    # on high-core-count machines (13 workers × 28 threads = 364 threads
    # on 28 logical CPUs → kernel scheduler drowns).
    parser.add_argument(
        "--max-workers", type=int, metavar="N",
        help="Cap chunk-parallel worker count (av1an's --workers). When "
             "omitted, computed from CPU topology (budget // 4 threads per "
             "worker, capped at physical_cores - 1). Set lower than the "
             "auto-computed value if the box hard-locks even with the "
             "thread cap, or higher if you have fast storage and want "
             "more parallelism. Combine with --threads-per-worker to "
             "fully override the auto math.",
    )
    parser.add_argument(
        "--threads-per-worker", type=int, metavar="N",
        help="Per-encoder thread cap (passed to SvtAv1EncApp / vpxenc / "
             "x265 via --video-params --threads N). When omitted, computed "
             "as max(1, budget // worker_count). Default behavior caps "
             "total active threads at logical_threads - 1 (one for OS/UI). "
             "Set higher if you have few large files and want each chunk "
             "to use more cores; set to 1 for maximum chunk parallelism "
             "on memory-bandwidth-bound workloads.",
    )
    # v4.2.0: --use-av1an opts INTO the av1an chunk-parallel path. The
    # default is now ffmpeg-only — av1an was too fragile across distros
    # (y4m pipe breaks, SvtAv1EncApp CLI quirks like rejecting --threads,
    # VapourSynth plugin issues, output buffering making it look hung).
    # ffmpeg's libsvtav1 is invoked as a library, accepts -threads
    # correctly, doesn't need VapourSynth, and produces immediate progress
    # output. av1an is still available for users who specifically want
    # scene-detection-based chunk-parallel encoding.
    parser.add_argument(
        "--use-av1an", action="store_true",
        help="Use av1an chunk-parallel encoding (opt-in). Default is "
             "ffmpeg-only, which is more reliable across distros. av1an "
             "requires VapourSynth + source plugins (lsmash/ffms2/"
             "bestsource) for fast chunk-parallel; without them it "
             "falls back to the slow 'select' chunk method. Only use "
             "--use-av1an if you have a working av1an+VapourSynth setup "
             "and want scene-detection-based chunk-parallel encoding.",
    )
    # v4.2.1: --verbose re-enables the tech-detail log output that v4.2.1
    # suppressed by default. Default is quiet — just per-file success/fail
    # + final summary. --verbose brings back the CMD: lines, live tail of
    # av1an/ffmpeg stderr, DIAGNOSIS blocks, resolution map, pre-flight
    # validation table, and the 30s heartbeat.
    parser.add_argument(
        "--verbose", action="store_true",
        help="Verbose log output. Default is quiet — only per-file "
             "success/fail + final summary. --verbose brings back the "
             "CMD: lines, live tail of av1an/ffmpeg stderr (frame= 67 "
             "fps= 12 ...), DIAGNOSIS blocks, resolution map, pre-flight "
             "validation table, and the 30s heartbeat.",
    )
    # v4.3.0: --skip-existing is the default. When the output file
    # already exists AND its video+audio codec matches the selected
    # encoder (verified via ffprobe), the file is skipped instead of
    # re-encoded. --force-reencode disables this for users who want
    # to re-encode at a different CRF/preset with the same codec.
    parser.add_argument(
        "--skip-existing", dest="skip_existing", action="store_true",
        default=True,
        help="Skip files whose output already exists with a matching "
             "video+audio codec (default). Probes the output with "
             "ffprobe and compares codec_name against the selected "
             "encoder. Skipped files are reported in the final summary "
             "as 'Skipped: N' and do NOT count as success or failure.",
    )
    parser.add_argument(
        "--force-reencode", dest="skip_existing", action="store_false",
        help="Re-encode every file, even if the output already exists "
             "with a matching codec. Use this when you want to change "
             "CRF/preset at the same codec — the skip-existing check "
             "doesn't verify encoder settings, only the codec itself.",
    )
    # v4.4.0: --timeout sets the per-file encode timeout (seconds).
    # Default 86400s = 24h, up from v4.0.0's 7200s = 2h. A 30GB 1080p
    # BluRay rip at SVT-AV1 preset 6 takes 4-10 hours; the old 2h
    # timeout killed massive-file encodes partway through. The STOP
    # button handles user-initiated aborts; this is just a safety net
    # for truly wedged processes.
    parser.add_argument(
        "--timeout", type=int, metavar="SECONDS", default=86400,
        help="Per-file encode timeout in seconds (default 86400 = 24h). "
             "A 30GB BluRay rip at SVT-AV1 preset 6 can take 4-10 hours; "
             "the old default (7200s = 2h) killed massive-file encodes. "
             "The STOP button handles user-initiated aborts; this timeout "
             "is just a safety net for truly wedged processes. Set to 0 "
             "for no timeout (not recommended — a wedged encode would "
             "hang the queue forever).",
    )
    # v4.4.4: --inline-scale skips the CRF-16 pre-scale intermediate when
    # a target resolution is selected. Instead, the scale/pad filter chain
    # is passed directly to av1an via --ffmpeg-filter-args. This eliminates
    # the 0.5-0.8× source size temp file (a 20GB source produced a 60GB
    # lossless intermediate under the old CRF-0 code, crashing the encode
    # with disk-exhaustion errors that presented as "ffmpeg error (rc=234)").
    # Default OFF — the intermediate path is more robust against av1an/
    # VapourSynth filter-arg quirks on older builds. Enable when scaling
    # large files (≥10GB) to avoid wasting disk and an extra encode pass.
    parser.add_argument(
        "--inline-scale", action="store_true",
        help="Skip the CRF-16 pre-scale intermediate. When a target "
             "resolution is selected, the scale/pad filter chain is "
             "passed directly to av1an via --ffmpeg-filter-args instead "
             "of pre-scaling to a temp file. Eliminates the 0.5-0.8x "
             "source size intermediate (was the cause of mysterious "
             "'ffmpeg error (rc=234)' failures on 10GB+ sources). "
             "Default OFF — the intermediate path is more robust on "
             "older av1an/VapourSynth builds. Enable for large files "
             "with scaling to save disk + an extra encode pass.",
    )
    return parser


def run_dry_run(
    chunk_method: str | None = None,
    max_workers: int | None = None,
    threads_per_worker: int | None = None,
) -> int:
    """Run the dry-run: probe env + smoke test, print report, return exit code."""
    # Deferred imports so --version never pulls in PySide6 or runs the
    # environment probe.
    from . import __version__
    from .env_probe import _av1an_vsscript_smoke_test, probe_environment

    print(f"opentranscode {__version__} — dry-run environment probe")
    print("=" * 60)

    env = probe_environment()

    # v4.0.0: --chunk-method CLI override takes precedence over the
    # env_probe auto-detection. "auto" means "let av1an decide" (clears
    # any override the probe set).
    cli_chunk_method_note = ""
    if chunk_method is not None:
        if chunk_method == "auto":
            env.av1an_flags.pop("chunk_method_override", None)
            cli_chunk_method_note = " (CLI: auto — cleared probe setting)"
        else:
            env.av1an_flags["chunk_method_override"] = chunk_method
            cli_chunk_method_note = f" (CLI: {chunk_method})"

    # v4.1.0: --max-workers / --threads-per-worker are stored on
    # env.av1an_flags so EncoderWorker picks them up via __init__'s
    # fallback path (no ui_window.py code changes needed).
    if max_workers is not None:
        env.av1an_flags["max_workers"] = max_workers
    if threads_per_worker is not None:
        env.av1an_flags["threads_per_worker"] = threads_per_worker

    print(f"Distro:        {env.distro.name} (family={env.distro.family}, "
          f"v{env.distro.version_id})")
    print(f"CPU:           {env.cpu.model_name} — "
          f"{env.cpu.physical_cores} physical / {env.cpu.logical_threads} logical")
    print(f"av1an:         {env.av1an_path or 'NOT FOUND'}"
          + (f" (v{env.av1an_version})" if env.av1an_version else ""))
    print(f"ffmpeg:        {env.ffmpeg_path or 'NOT FOUND'}"
          + (f" (v{env.ffmpeg_version})" if env.ffmpeg_version else ""))
    print(f"ffprobe:       {env.ffprobe_path or 'NOT FOUND'}")
    print(f"VapourSynth:   {env.vs_version or 'NOT FOUND'}"
          + (f" ({env.vs_script_lib})" if env.vs_script_lib else ""))
    # v4.0.0: show VS source plugins + effective chunk method
    vs_plugins = env.av1an_flags.get("vs_plugins", [])
    if vs_plugins:
        print(f"VS plugins:    {', '.join(vs_plugins)}")
    else:
        print(f"VS plugins:    (none — Hybrid chunk method will fail on "
              f"phone-recorded MP4s)")
    effective_cm = env.av1an_flags.get("chunk_method_override")
    print(f"Chunk method:  {effective_cm or 'auto (av1an decides)'}{cli_chunk_method_note}")

    # v4.1.0: show intelligent worker math so the user can verify the
    # chunk-parallel thread budget before launching a real encode.
    # We instantiate EncoderWorker without starting the QThread to read
    # the computed values — __init__ doesn't touch Qt, only sets attrs.
    try:
        from .encoder_worker import EncoderWorker
        from .codec_profiles import VIDEO_CODECS, AUDIO_PROFILES, CONTAINER_PROFILES, RESOLUTION_PRESETS
        from pathlib import Path
        # Use a stub in_dir/out_dir — run() is never called, only the
        # _compute_intelligent_worker_count method is invoked.
        probe_worker = EncoderWorker(
            in_dir=Path("/tmp"),
            out_dir=Path("/tmp"),
            video_codec=VIDEO_CODECS[0],
            audio_profile=AUDIO_PROFILES[0],
            container=CONTAINER_PROFILES[0],
            crf=30,
            preset_label="Medium (6)",
            delete_source=False,
            env=env,
            extensions={".mkv"},
            resolution=RESOLUTION_PRESETS[0],
            max_workers=max_workers,
            threads_per_worker=threads_per_worker,
        )
        wc, tpw = probe_worker._compute_intelligent_worker_count()
        active = wc * tpw
        reserved = max(0, env.cpu.logical_threads - active)
        overrides = []
        if max_workers is not None:
            overrides.append(f"--max-workers={max_workers}")
        if threads_per_worker is not None:
            overrides.append(f"--threads-per-worker={threads_per_worker}")
        override_note = f" (overrides: {', '.join(overrides)})" if overrides else " (auto)"
        print(f"Workers:       {wc} workers × {tpw} threads = {active} active"
              f" — {reserved} reserved for OS/UI{override_note}")
    except Exception as e:
        # Don't fail the dry-run if the worker probe hits an edge case.
        print(f"Workers:       (could not compute: {e})")

    print("ffmpeg libs:   " + ", ".join(
        f"{k}={'yes' if v else 'no'}" for k, v in sorted(env.ffmpeg_libs.items())
    ))
    if env.errors:
        print("\nERRORS:")
        for e in env.errors:
            print(f"  - {e}")
    if env.warnings:
        print("\nWARNINGS:")
        for w in env.warnings:
            print(f"  - {w}")

    # Smoke test only if av1an + ffmpeg are both present.
    if env.av1an_path and env.ffmpeg_path:
        print("\n--- av1an VSScript smoke test ---")
        svt_name = (env.av1an_flags or {}).get("svt_name", "svt_av1")
        ok, detail = _av1an_vsscript_smoke_test(
            env.av1an_path, env.ffmpeg_path, env.av1an_flags, svt_name,
        )
        print(f"  result: {'OK' if ok else 'FAIL'}")
        print(f"  detail: {detail}")
        if not ok:
            print("\nDry-run complete — smoke test FAILED.")
            return 1
    else:
        print("\nSmoke test skipped (av1an or ffmpeg not found).")

    print("\nDry-run complete.")
    return 0 if not env.errors else 1


def run_verify_only(path: str) -> int:
    """Re-verify an existing output file via ffprobe (no re-encode)."""
    import os

    from .ffprobe_utils import ffprobe_duration, ffprobe_validate

    target = Path(path)
    if not target.is_file():
        print(f"verify-only: file not found: {target}", file=sys.stderr)
        return 1

    ffprobe_bin = os.environ.get("FFPROBE_BIN", "ffprobe")
    info = ffprobe_validate(target, ffprobe_bin)
    if info is None:
        print(f"verify-only: ffprobe could not read {target}", file=sys.stderr)
        return 1

    size = target.stat().st_size
    duration = ffprobe_duration(target, ffprobe_bin)
    streams = info.get("streams", [])
    vstream = next((s for s in streams if s.get("codec_type") == "video"), {})
    width = vstream.get("width", "?")
    height = vstream.get("height", "?")

    print(f"file:      {target}")
    print(f"size:      {size} bytes ({size / 1024 / 1024:.2f} MiB)")
    print(f"duration:  {duration if duration is not None else '?'} s"
          if duration is not None else "duration:  ?")
    print(f"resolution: {width}x{height}")
    print("\nverify-only: OK" if size > 0 else "\nverify-only: FAIL (empty file)")
    return 0 if size > 0 else 1


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code."""
    args = build_parser().parse_args(argv)
    if args.version:
        from . import __version__
        print(f"opentranscode {__version__}")
        return 0
    if args.dry_run:
        return run_dry_run(
            chunk_method=args.chunk_method,
            max_workers=args.max_workers,
            threads_per_worker=args.threads_per_worker,
        )
    if args.verify_only:
        return run_verify_only(args.verify_only)
    # No flag (or --force) — launch GUI. --force pre-checks the Force
    # checkbox; the user can still toggle it in the UI.
    # v4.0.0: --chunk-method sets env.av1an_flags["chunk_method_override"]
    # before the GUI launches so every EncoderWorker picks it up.
    # v4.1.0: --max-workers / --threads-per-worker do the same — stored
    # on env.av1an_flags and picked up by EncoderWorker.__init__'s
    # fallback path (no ui_window.py changes needed).
    from .ui_window import launch_gui
    return launch_gui(
        force=args.force,
        chunk_method=args.chunk_method,
        max_workers=args.max_workers,
        threads_per_worker=args.threads_per_worker,
        use_av1an=args.use_av1an,
        verbose=args.verbose,
        skip_existing=args.skip_existing,
        timeout=args.timeout,
        inline_scale=args.inline_scale,
    )
