"""Hybrid GPU+CPU batch scheduler (v4.7.0).

Splits a transcode queue between two concurrent lanes so the NVENC
engine and the CPU encoders work at the same time instead of leaving
28 Xeon threads idle while the GPU encode runs:

  - GPU lane: files encoded via the functional NVENC encoder
    (hevc_nvenc / av1_nvenc) through the single-pass ffmpeg path.
  - CPU lane: the remaining files through the family's software encoder
    (libx265 / libsvtav1 / libvpx) or av1an chunk-parallel if the user
    opted in — so "all three" (NVENC + software + chunk-parallel) run
    side by side when av1an is enabled.

Pure planning logic — no Qt, no I/O beyond the caller-provided file
sizes. The UI turns a HybridPlan into two EncoderWorker instances.

Scope note: lanes split FILES, never one file across encoders. Splitting
a single file between hevc_nvenc and libx265 chunks would produce
visibly inconsistent quality between scenes, and av1an cannot drive
NVENC at all (it spawns encoder CLI binaries only).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# NVENC HEVC on a Pascal card runs several times faster than x265
# "faster" on 28 Xeon threads. Used only for load ESTIMATION (which lane
# gets the next file) — a wrong guess just skews the split slightly.
GPU_SPEED_RATIO_DEFAULT = 8

# Threads held back from the CPU lane so the GPU lane's decode / scale /
# mux processes stay responsive. The NVENC encode itself runs on the GPU
# silicon; the CPU side of a nvenc job is light.
HYBRID_CPU_RESERVE_THREADS = 2


@dataclass
class HybridPlan:
    """Result of planning a hybrid (GPU + CPU) queue split."""

    gpu_files: list[Path] = field(default_factory=list)
    cpu_files: list[Path] = field(default_factory=list)
    gpu_encoder: str = ""                 # e.g. "hevc_nvenc"
    cpu_budget_threads: int = 1           # CPU lane thread budget (logical - reserve)
    gpu_speed_ratio: int = GPU_SPEED_RATIO_DEFAULT

    @property
    def total_files(self) -> int:
        return len(self.gpu_files) + len(self.cpu_files)


def plan_hybrid(
    files: list[Path],
    gpu_encoder: str | None,
    gpu_functional: bool,
    logical_threads: int,
    sizes: dict[Path, int] | None = None,
    gpu_speed_ratio: int = GPU_SPEED_RATIO_DEFAULT,
    cpu_reserve: int = HYBRID_CPU_RESERVE_THREADS,
) -> HybridPlan | None:
    """Split *files* between the GPU and CPU lanes, or return None when a
    hybrid split cannot apply.

    Returns None when:
      - the codec family has no GPU encoder, or the live GPU probe failed
        (caller should fall back to a plain CPU queue), or
      - *files* is empty.

    Assignment is LPT (longest-processing-time first): files are sorted
    by size descending and each goes to the lane with the lower
    estimated load, where the GPU lane's per-file cost is size /
    gpu_speed_ratio. Both lanes then finish at roughly the same time.

    *sizes* maps files to byte sizes; missing entries fall back to the
    mean of the known sizes (or 10 MB when nothing is known) so a single
    unreadable file cannot skew the whole split.
    """
    if not gpu_encoder or not gpu_functional or not files:
        return None

    sizes = sizes or {}
    known = [s for s in sizes.values() if s]
    avg = sum(known) // len(known) if known else 10_000_000

    def size_of(f: Path) -> int:
        return sizes.get(f) or avg

    ratio = max(1, int(gpu_speed_ratio))
    gpu_files: list[Path] = []
    cpu_files: list[Path] = []
    gpu_load = 0.0
    cpu_load = 0.0

    for f in sorted(files, key=size_of, reverse=True):
        s = size_of(f)
        gpu_est = gpu_load + s / ratio
        cpu_est = cpu_load + s
        # Tie goes to the GPU lane — it finishes the file sooner and the
        # CPU lane keeps its current file longer.
        if gpu_est <= cpu_est:
            gpu_files.append(f)
            gpu_load = gpu_est
        else:
            cpu_files.append(f)
            cpu_load = cpu_est

    return HybridPlan(
        gpu_files=gpu_files,
        cpu_files=cpu_files,
        gpu_encoder=gpu_encoder,
        cpu_budget_threads=max(1, int(logical_threads) - cpu_reserve),
        gpu_speed_ratio=ratio,
    )
