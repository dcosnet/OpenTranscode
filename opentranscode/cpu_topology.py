"""CPU topology detection (physical cores, not hyperthreads).

Reads /sys/devices/system/cpu/* and falls back to ``lscpu``. Pure
stdlib; no internal package dependencies.
"""

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# ──────────────────────────────────────────────
#  CPU TOPOLOGY (physical cores, not hyperthreads)
# ──────────────────────────────────────────────

@dataclass
class CpuTopology:
    physical_cores: int
    logical_threads: int
    threads_per_core: int
    model_name: str


def _read_sysfs_cores() -> (tuple[int, int]) | None:
    """
    Read /sys/devices/system/cpu/cpu*/topology/ to count unique
    (physical_package_id, core_id) pairs — i.e. physical cores.
    Returns (physical_cores, logical_threads) or None.
    """
    cpu_base = Path("/sys/devices/system/cpu")
    if not cpu_base.exists():
        return None

    unique_cores: set[tuple[str, str]] = set()
    logical = 0
    for cpu_dir in sorted(cpu_base.glob("cpu[0-9]*")):
        core_id_file = cpu_dir / "topology" / "core_id"
        pkg_id_file = cpu_dir / "topology" / "physical_package_id"
        if core_id_file.exists() and pkg_id_file.exists():
            try:
                pkg = pkg_id_file.read_text().strip()
                core = core_id_file.read_text().strip()
                unique_cores.add((pkg, core))
                logical += 1
            except (OSError, ValueError):
                # OSError: file vanished/permission; ValueError: UnicodeDecodeError
                pass
    if unique_cores and logical:
        return (len(unique_cores), logical)
    return None


def _read_lscpu_cores() -> (tuple[int, int]) | None:
    """Fallback: parse lscpu -p=CORE,SOCKET for unique physical cores."""
    if not shutil.which("lscpu"):
        return None
    try:
        res = subprocess.run(
            ["lscpu", "-p=CORE,SOCKET"],
            capture_output=True, text=True, timeout=5,
        )
        lines = [l.strip() for l in res.stdout.strip().splitlines() if l.strip() and not l.startswith("#")]
        if lines:
            unique = set(lines)
            return (len(unique), len(lines))
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def detect_cpu_topology() -> CpuTopology:
    """
    Detect physical CPU topology. Prefers /sys filesystem, falls back
    to lscpu, then estimates from os.cpu_count().
    """
    logical = os.cpu_count() or 1
    physical = logical

    # Try /sys first (most reliable)
    result = _read_sysfs_cores()
    if result:
        physical, logical = result
    else:
        # Try lscpu
        result = _read_lscpu_cores()
        if result:
            physical, logical = result
        else:
            # Estimate: assume 2 threads/core if cpu_count > 2 and is even
            if logical > 2 and logical % 2 == 0:
                physical = logical // 2

    tpc = logical // physical if physical > 0 else 1

    # Try to get CPU model name
    model = "Unknown CPU"
    model_file = Path("/proc/cpuinfo")
    if model_file.exists():
        for line in model_file.read_text(errors="replace").splitlines():
            if line.startswith("model name"):
                model = line.split(":", 1)[1].strip()
                break
    else:
        # Non-x86 / non-Linux: try lscpu
        if shutil.which("lscpu"):
            try:
                res = subprocess.run(["lscpu"], capture_output=True, text=True, timeout=5)
                for line in res.stdout.splitlines():
                    if "Model name" in line:
                        model = line.split(":", 1)[1].strip()
                        break
            except (OSError, subprocess.SubprocessError):
                pass

    return CpuTopology(
        physical_cores=physical,
        logical_threads=logical,
        threads_per_core=tpc,
        model_name=model,
    )

