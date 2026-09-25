"""GPU capability profiles (v4.8.0) — combined card generations.

Cards within the same hardware-encoder generation are functionally
identical for transcoding, so the dropdown lists CAPABILITY CLASSES,
not individual SKUs: one Pascal entry covers the GTX 10-series, Tesla
P40/P4/P100 and mobile chips; one Turing entry covers RTX 20-series,
GTX 16-series, the Tesla T4 and the crypto-era CMP 30/40/50HX cards.

Oddballs are included with their real capabilities:
  - CMP 90HX is GA102-based (Ampere NVENC), but CMP 170HX is GA100-based
    and has NO NVENC at all (like A100/V100/H100 compute boards).
  - Intel Arc (QSV) and AMD RDNA (VAAPI) cover the rest of the trending
    list; RDNA 3 added AV1 encode, RDNA 1/2 and GCN can only encode
    H.264/HEVC.

Pure data + pure functions: no I/O, safe to import anywhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ──────────────────────────────────────────────
#  GPU PROFILES
# ──────────────────────────────────────────────

@dataclass(frozen=True)
class GpuProfile:
    key: str                    # stable id (CLI/UI)
    label: str                  # dropdown entry
    vendor: str                 # nvidia | amd | intel | none
    api: str                    # nvenc | vaapi | qsv | none
    # codec family → ffmpeg encoder name
    encoders: dict[str, str]
    # case-insensitive substrings matched against the detected GPU name
    # (nvidia-smi / lspci) for auto-detection. First match wins; lists
    # are ordered most-specific first.
    match: tuple[str, ...] = ()
    notes: str = ""
    # extra args that must come BEFORE -i (hardware device init)
    hw_device_args: tuple[str, ...] = ()
    # filter-chain fragment required before the encoder (vaapi hwupload)
    filter_tail: tuple[str, ...] = ()


# NVENC encoders by generation class. Quality control: -rc vbr -cq N
# (-b:v 0). 8-bit yuv420p everywhere — Pascal 10-bit HEVC runs at half
# speed and the archival targets here are 8-bit sources.
_NV = {"h264": "h264_nvenc", "hevc": "hevc_nvenc"}

GPU_PROFILES: list[GpuProfile] = [
    GpuProfile(
        key="nv-kepler-maxwell",
        label="NVIDIA Kepler / Maxwell 1.0 (GTX 600/700/800M) — H.264 only",
        vendor="nvidia", api="nvenc",
        encoders={"h264": "h264_nvenc"},
        match=("GTX 6", "GTX 7", "GT 7", "GTX 8", "GT 8", "840M", "860M", "750"),
        notes="First NVENC generations: H.264 only, no HEVC.",
    ),
    GpuProfile(
        key="nv-pascal",
        label="NVIDIA Pascal (GTX 10-series, TITAN Xp, Tesla P40/P4/P100) — H.264 + HEVC 8/10-bit",
        vendor="nvidia", api="nvenc",
        encoders=dict(_NV),
        match=("GTX 10", "1070", "1080", "1060", "1050", "TITAN Xp",
               "Tesla P40", "Tesla P4", "P100", "Quadro P"),
        notes="Pascal NVENC: HEVC Main/Main10. 10-bit runs at ~half speed.",
    ),
    GpuProfile(
        key="nv-turing",
        label="NVIDIA Turing (RTX 20-series, GTX 16-series, Tesla T4, CMP 30/40/50HX) — H.264 + HEVC + B-frames",
        vendor="nvidia", api="nvenc",
        encoders=dict(_NV),
        match=("RTX 20", "GTX 16", "2060", "2070", "2080", "1660", "1650",
               "Tesla T4", "CMP 30", "CMP 40", "CMP 50"),
        notes="Turing NVENC: first gen with HEVC B-frames; big quality jump.",
    ),
    GpuProfile(
        key="nv-compute",
        label="NVIDIA data-center compute (V100/A100/H100, CMP 170HX) — no NVENC (CPU path)",
        vendor="nvidia", api="none",
        encoders={},
        match=("V100", "A100", "H100", "B200", "GB200", "CMP 170"),
        notes="Compute boards ship without NVENC silicon. CMP 170HX is "
              "GA100-based — the fastest mining card that cannot hardware-encode.",
    ),
    GpuProfile(
        key="nv-ampere",
        label="NVIDIA Ampere (RTX 30-series, A10/A40/A2, CMP 90HX) — H.264 + HEVC (no AV1 encode)",
        vendor="nvidia", api="nvenc",
        encoders=dict(_NV),
        match=("RTX 30", "3090", "3080", "3070", "3060", "3050",
               "A10", "A40", "CMP 90"),
        notes="Ampere added AV1 DECODE but not encode — AV1 stays on CPU.",
    ),
    GpuProfile(
        key="nv-ada",
        label="NVIDIA Ada / Blackwell (RTX 40/50-series, L4/L40) — H.264 + HEVC + AV1 10-bit",
        vendor="nvidia", api="nvenc",
        encoders={"h264": "h264_nvenc", "hevc": "hevc_nvenc", "av1": "av1_nvenc"},
        match=("RTX 40", "RTX 50", "4090", "4080", "4070", "4060",
               "5090", "5080", "5070", "5060", "L4", "L40"),
        notes="Ada introduced AV1 NVENC; Blackwell doubles AV1 throughput.",
    ),
    GpuProfile(
        key="intel-arc",
        label="Intel Arc (Alchemist A-series, Battlemage B-series) — QSV: H.264 + HEVC + AV1",
        vendor="intel", api="qsv",
        encoders={"h264": "h264_qsv", "hevc": "hevc_qsv", "av1": "av1_qsv"},
        match=("Arc A", "Arc B", "A380", "A750", "A770", "B570", "B580"),
        notes="Arc media engines encode AV1 8/10-bit — best value encode card.",
    ),
    GpuProfile(
        key="intel-xe",
        label="Intel Iris / UHD integrated (Gen9–Xe) — QSV: H.264 + HEVC",
        vendor="intel", api="qsv",
        encoders={"h264": "h264_qsv", "hevc": "hevc_qsv"},
        match=("Iris", "UHD", "HD Graphics"),
        notes="Integrated media engines; HEVC 8/10-bit, no AV1 encode.",
    ),
    GpuProfile(
        key="amd-rdna3",
        label="AMD RDNA 3 (RX 7000-series) — VAAPI: H.264 + HEVC + AV1",
        vendor="amd", api="vaapi",
        encoders={"h264": "h264_vaapi", "hevc": "hevc_vaapi", "av1": "av1_vaapi"},
        match=("RX 7", "7900", "7800", "7700", "7600"),
        notes="RDNA 3 VCN: first AMD generation with AV1 encode.",
    ),
    GpuProfile(
        key="amd-rdna12",
        label="AMD RDNA 1/2 (RX 5000/6000-series) — VAAPI: H.264 + HEVC (AV1 decode only)",
        vendor="amd", api="vaapi",
        encoders={"h264": "h264_vaapi", "hevc": "hevc_vaapi"},
        match=("RX 5", "RX 6", "5700", "5600", "6800", "6700", "6600", "6500"),
        notes="RDNA 2 has AV1 decode only — AV1 encode stays on CPU.",
    ),
    GpuProfile(
        key="amd-gcn",
        label="AMD GCN 4/5 / Vega (RX 400/500, Vega 56/64) — VAAPI: H.264 + HEVC",
        vendor="amd", api="vaapi",
        encoders={"h264": "h264_vaapi", "hevc": "hevc_vaapi"},
        match=("RX 4", "RX 5", "Vega", "580", "570", "480", "470", "64", "56"),
        notes="The classic crypto-era mining cards (Polaris/Vega).",
    ),
    GpuProfile(
        key="cpu",
        label="None (CPU-only encode)",
        vendor="none", api="none",
        encoders={},
    ),
]

_GPU_PROFILES_BY_KEY: dict[str, GpuProfile] = {p.key: p for p in GPU_PROFILES}


def gpu_profile_by_key(key: str | None) -> GpuProfile | None:
    if not key:
        return None
    return _GPU_PROFILES_BY_KEY.get(key)


def match_gpu_profile(gpu_name: str) -> GpuProfile | None:
    """Best-effort auto-detection from a GPU name string (nvidia-smi or
    lspci output). Case-insensitive; first matching profile wins (the
    match lists are ordered most-specific first, and the compute boards
    are matched before the consumer generations they share names with —
    e.g. 'CMP 170HX' must not hit the Ampere 'A10' style entries)."""
    if not gpu_name:
        return None
    name = gpu_name.lower()
    for profile in GPU_PROFILES:
        for frag in profile.match:
            if frag.lower() in name:
                return profile
    return None


# ──────────────────────────────────────────────
#  FFMPEG ARG HELPERS (per hardware API)
# ──────────────────────────────────────────────

def encoder_for_family(profile: GpuProfile | None, family: str) -> str | None:
    """Hardware encoder name for a codec family on this profile, or None."""
    if not profile or profile.api == "none":
        return None
    return profile.encoders.get(family)


def resolve_vaapi_device() -> str:
    """First render node, or the classic fallback path. (Best-effort I/O —
    callers that need purity pass the result into encoder_pre_args.)"""
    import glob
    nodes = sorted(glob.glob("/dev/dri/renderD*"))
    return nodes[0] if nodes else "/dev/dri/renderD128"


def encoder_pre_args(profile: GpuProfile, vaapi_device: str | None = None) -> list[str]:
    """Args that must precede -i (hardware device initialisation)."""
    if profile.api == "vaapi":
        dev = vaapi_device or resolve_vaapi_device()
        return ["-vaapi_device", dev]
    if profile.api == "qsv":
        return ["-init_hw_device", "qsv=hw"]
    return []


def encoder_filter_chain(profile: GpuProfile) -> list[str]:
    """Filter args that upload software frames to the hardware surface
    format (VAAPI encoders only accept hw frames; -vaapi_device makes
    its device the default for hwupload)."""
    if profile.api == "vaapi":
        return ["-vf", "format=nv12,hwupload"]
    return []


def encoder_quality_args(api: str, encoder: str, crf: int, preset: int) -> list[str]:
    """Constant-quality args for a hardware encoder. NVENC maps the CPU
    preset tiers to p-presets; QSV uses very_fast/medium; VAAPI uses CQP
    rate mode which has no preset knob."""
    if api == "nvenc":
        if preset <= 6:
            p = "p7"
        elif preset <= 8:
            p = "p5"
        else:
            p = "p4"
        return ["-preset", p, "-tune", "hq", "-rc", "vbr",
                "-cq", str(crf), "-b:v", "0", "-pix_fmt", "yuv420p",
                "-g", "240"]
    if api == "qsv":
        p = "veryslow" if preset <= 6 else ("medium" if preset <= 8 else "very_fast")
        return ["-preset", p, "-global_quality", str(crf),
                "-pix_fmt", "yuv420p", "-g", "240"]
    if api == "vaapi":
        return ["-rc_mode", "CQP", "-qp", str(crf), "-g", "240"]
    return []
