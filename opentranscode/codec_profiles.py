"""Codec / audio / container profile tables and helpers.

Data-driven configuration that replaces the v1 if/else codec chains.
Pure data + pure functions — no PySide6, no I/O, no internal package
dependencies. Safe to import from any context (incl. unit tests and
the CLI --version path).
"""

from collections.abc import Callable
from dataclasses import dataclass, field

# ──────────────────────────────────────────────
#  CONFIG-DRIVEN PROFILES (replaces all if/else chains)
# ──────────────────────────────────────────────

@dataclass
class VideoCodecProfile:
    label: str                   # Display name in combo box
    av1an_encoder: str           # Encoder name passed to --encoder
    ffmpeg_encoder: str          # Encoder name for pure-ffmpeg fallback (e.g. "libsvtav1")
    container: str               # Default container extension (mkv or webm)
    crf_range: tuple[int, int]   # (min, max) valid CRF values
    default_crf: int
    # (crf, preset) -> av1an --video-params string. Passed to SvtAv1EncApp /
    # vpxenc / x265 as a CLI invocation, so ONLY CLI-accepted flags may
    # appear here. Thread capping lives in ffmpeg_vargs_fn (where
    # libsvtav1 is invoked as a library and accepts -threads) and in
    # EncoderWorker's --workers count (av1an's chunk-parallel knob).
    params_fn: Callable[[int, int], str]
    ffmpeg_vargs_fn: Callable[[int, int], list[str]]  # (crf, preset) -> ffmpeg -c:v args
    presets: list[str]           # Human-readable preset labels
    preset_map: dict[str, int]   # label -> internal preset value
    # v4.3.0: the codec_name ffprobe returns for files encoded with this
    # profile. Used by _output_already_encoded() to detect skip-existing.
    # av1 → "av1", vp9 → "vp9", hevc → "hevc". Verified against ffprobe
    # output for each encoder; this is the codec_name field in the video
    # stream's JSON, NOT the encoder_name (which would be "libsvtav1" etc).
    ffprobe_codec_name: str = ""
    # v4.6.0: hardware (NVENC) counterpart for this codec family. Empty
    # string = no hardware encoder exists for this family (VP9 has no
    # NVENC encoder). The GPU path is ffmpeg-only (av1an cannot drive
    # NVENC); EncoderWorker.resolve_gpu_encoder() only selects it when a
    # functional probe proved the encoder works on this system. The
    # ffprobe codec_name is IDENTICAL to the CPU encoder's (hevc_nvenc
    # also produces "hevc"), so skip-existing detection works across
    # GPU/CPU re-encodes of the same family.
    gpu_encoder: str = ""
    # (crf, preset) -> ffmpeg args for the NVENC encoder. Mirrors
    # ffmpeg_vargs_fn. None when gpu_encoder is empty.
    gpu_vargs_fn: Callable[[int, int], list[str]] | None = None
    # v4.8.0: GPU-profile support. *gpu_family* is the codec family key
    # used by GpuProfile.encoders ("av1"/"hevc"/"vp9"); *gpu_encoders_by_api*
    # maps a hardware API (nvenc/vaapi/qsv) to this profile's ffmpeg
    # encoder for that API. resolve_gpu_encoder() picks the entry matching
    # the selected GPU profile.
    gpu_family: str = ""
    gpu_encoders_by_api: dict[str, str] = field(default_factory=dict)

@dataclass
class AudioProfile:
    label: str
    params: list[str]            # Tokens passed to --audio-params (joined with space)
    # v3 (OTC-012, SEI CERT STR09-C): the ffmpeg audio encoder name this
    # profile depends on, e.g. "libopus", "libvorbis", "flac", "libiamf".
    # Used by _check_combo_compatibility and _disable_unavailable_codecs
    # to look up the encoder directly in EnvProbe.ffmpeg_libs — replacing
    # the v2 substring match (`"libiamf" in ap.params`) which would
    # falsely match a hypothetical `-libiamf-mode` argument.
    # Empty string means "no ffmpeg encoder dependency" (rare; only used
    # by passthrough profiles that don't transcode audio).
    ffmpeg_encoder_name: str = ""
    # v4.3.0: the codec_name ffprobe returns for files encoded with this
    # profile. Used by _output_already_encoded() to detect skip-existing.
    # opus → "opus", vorbis → "vorbis", flac → "flac", iamf → "iamf".
    ffprobe_codec_name: str = ""

@dataclass
class ContainerProfile:
    label: str
    ext: str                     # e.g. "mkv", "webm"


def _av1_params(crf: int, preset: int) -> str:
    """SVT-AV1 encoder params for av1an's --video-params.

    av1an splits the --video-params value by whitespace (``split_whitespace()``)
    and passes each resulting token as a separate argument to SvtAv1EncApp.
    Therefore the string must contain space-separated ``--flag value`` pairs
    that SvtAv1EncApp can parse natively.

    Colon-separated ``key=value:key=value`` does NOT work because there are
    no whitespace boundaries for av1an to split on — the entire string reaches
    SvtAv1EncApp as one opaque argument, producing:
    ``Maybe missing spacing between tokens``.

    Thread capping is NOT injected here. SvtAv1EncApp (the standalone CLI
    av1an invokes per-chunk) uses `--lp N` (logical processors), not
    `--threads N`. Thread capping is handled via av1an's `--workers` flag
    (chunk-parallel count) and via `-threads` in the ffmpeg fallback path
    (where libsvtav1 is a library and accepts it).
    """
    return f"--preset {preset} --crf {crf} --keyint 240"


def _vp9_params(crf: int, preset: int) -> str:
    """VP9 encoder params for av1an's --video-params.

    av1an splits by whitespace, so we use space-separated --flag=value tokens
    that vpxenc parses natively.
    """
    cpu_used = max(0, 8 - preset)
    return f"--end-usage=q --cq-level={crf} --cpu-used={cpu_used}"


def _x265_params(crf: int, preset: int) -> str:
    """x265 encoder params for av1an's --video-params.

    av1an splits by whitespace, so we use space-separated --flag value tokens
    that x265 parses natively.
    """
    return f"--crf {crf} --preset {preset}"

def _svtav1_ffmpeg_args(crf: int, preset: int) -> list[str]:
    """FFmpeg args for SVT-AV1 (maps av1an preset=0..8 → svtav1 -preset 0..13)."""
    # av1an preset range 0-8 maps to SVT-AV1 preset range 0-13
    # Scale roughly: 8→0, 6→4, 4→7, 2→10
    svt_preset = max(0, min(13, round((8 - preset) * 13 / 8)))
    return ["-c:v", "libsvtav1", "-preset", str(svt_preset), "-crf", str(crf),
            "-pix_fmt", "yuv420p10le", "-g", "240"]

def _vp9_ffmpeg_args(crf: int, preset: int) -> list[str]:
    """FFmpeg args for VP9 (maps av1an cpu-used 0..8 → -cpu-used 0..8)."""
    cpu_used = max(0, min(8, preset))
    return ["-c:v", "libvpx-vp9", "-crf", str(crf), "-b:v", "0",
            "-cpu-used", str(cpu_used), "-pix_fmt", "yuv420p", "-g", "240",
            "-row-mt", "1", "-tiles", "2x2"]

def _x265_ffmpeg_args(crf: int, preset: int) -> list[str]:
    """FFmpeg args for x265 (maps av1an preset 5..10 → x265 -preset)."""
    # av1an x265 preset range 5-10 maps to x265 preset names
    preset_names = {5: "slow", 7: "medium", 9: "fast", 10: "faster"}
    p = preset_names.get(preset, "medium")
    return ["-c:v", "libx265", "-preset", p, "-crf", str(crf),
            "-pix_fmt", "yuv420p10le", "-g", "240"]


# ── v4.6.0: NVENC (hardware) vargs ──
# NVENC quality control: -rc vbr + -cq N + -b:v 0 is the constant-quality
# mode that maps most closely to the CPU encoders' CRF (cq ≈ crf for HEVC
# and AV1 within ~±3). -b:v 0 removes the default bitrate cap so -cq
# actually governs quality. Presets are p1 (fastest) .. p7 (slowest/best)
# on all current NVENC generations; the legacy "slow/medium/fast" aliases
# are deprecated.
#
# Pixel format: 8-bit yuv420p. Pascal-generation cards (GTX 10xx) run
# HEVC Main10 at roughly half throughput, and the archival targets here
# are 8-bit phone/BluRay sources — 8-bit keeps the GPU path at full
# speed. ffmpeg auto-converts 10-bit sources to yuv420p.

def _nvenc_preset(preset: int) -> str:
    """Map the CPU preset tiers (lower value = slower/better) to NVENC
    p-presets. CPU preset values across profiles are 0..10 with 0/5 =
    slowest quality tiers; NVENC is fast enough that even p7 outruns any
    CPU encoder, so the whole range compresses to p3..p7."""
    if preset <= 6:
        return "p7"   # "Slow" tier → best NVENC quality
    if preset <= 8:
        return "p5"   # "Medium" tier
    return "p4"       # "Fast"/"Faster" tiers


def _hevc_nvenc_args(crf: int, preset: int) -> list[str]:
    """FFmpeg args for hevc_nvenc (x265/HEVC family hardware encoder)."""
    return ["-c:v", "hevc_nvenc", "-preset", _nvenc_preset(preset),
            "-tune", "hq", "-rc", "vbr", "-cq", str(crf), "-b:v", "0",
            "-pix_fmt", "yuv420p", "-g", "240"]


def _h264_nvenc_args(crf: int, preset: int) -> list[str]:
    """FFmpeg args for h264_nvenc (hardware H.264 — compatibility target)."""
    return ["-c:v", "h264_nvenc", "-preset", _nvenc_preset(preset),
            "-tune", "hq", "-rc", "vbr", "-cq", str(crf), "-b:v", "0",
            "-pix_fmt", "yuv420p", "-g", "240"]


def _av1_nvenc_args(crf: int, preset: int) -> list[str]:
    """FFmpeg args for av1_nvenc (AV1 family hardware encoder, RTX 40+)."""
    return ["-c:v", "av1_nvenc", "-preset", _nvenc_preset(preset),
            "-tune", "hq", "-rc", "vbr", "-cq", str(crf), "-b:v", "0",
            "-pix_fmt", "yuv420p", "-g", "240"]


VIDEO_CODECS: list[VideoCodecProfile] = [
    VideoCodecProfile(
        label="AV1 (SVT-AV1)",
        av1an_encoder="svt_av1",
        ffmpeg_encoder="libsvtav1",
        container="mkv",
        crf_range=(18, 52),
        default_crf=32,
        params_fn=_av1_params,
        ffmpeg_vargs_fn=_svtav1_ffmpeg_args,
        presets=["Slow (8)", "Medium (6)", "Fast (4)", "Faster (2)"],
        preset_map={"Slow (8)": 8, "Medium (6)": 6, "Fast (4)": 4, "Faster (2)": 2},
        ffprobe_codec_name="av1",  # v4.3.0: skip-existing detection
        # v4.6.0: av1_nvenc exists only on RTX 40+ (Ada) cards; on Pascal
        # (GTX 10xx) the functional probe fails and auto falls back to
        # the SVT-AV1 CPU encoder.
        gpu_encoder="av1_nvenc",
        gpu_vargs_fn=_av1_nvenc_args,
        gpu_family="av1",
        gpu_encoders_by_api={"nvenc": "av1_nvenc", "qsv": "av1_qsv",
                             "vaapi": "av1_vaapi"},
    ),
    VideoCodecProfile(
        label="VP9",
        av1an_encoder="vpx",
        ffmpeg_encoder="libvpx-vp9",
        container="webm",
        crf_range=(18, 52),
        default_crf=32,
        params_fn=_vp9_params,
        ffmpeg_vargs_fn=_vp9_ffmpeg_args,
        presets=["Slow (0)", "Medium (2)", "Fast (4)", "Faster (6)"],
        preset_map={"Slow (0)": 0, "Medium (2)": 2, "Fast (4)": 4, "Faster (6)": 6},
        ffprobe_codec_name="vp9",  # v4.3.0: skip-existing detection
        # v4.8.0: VP9 has no NVENC encoder; VAAPI (AMD/older Intel) can
        # encode it on some cards.
        gpu_family="vp9",
        gpu_encoders_by_api={"vaapi": "vp9_vaapi"},
    ),
    VideoCodecProfile(
        label="x265 (HEVC)",
        av1an_encoder="x265",
        ffmpeg_encoder="libx265",
        container="mkv",
        crf_range=(18, 40),
        default_crf=28,
        params_fn=_x265_params,
        ffmpeg_vargs_fn=_x265_ffmpeg_args,
        presets=["Slow (5)", "Medium (7)", "Fast (9)", "Faster (10)"],
        preset_map={"Slow (5)": 5, "Medium (7)": 7, "Fast (9)": 9, "Faster (10)": 10},
        ffprobe_codec_name="hevc",  # v4.3.0: skip-existing detection
        # v4.6.0: hevc_nvenc works on every NVENC generation since Maxwell
        # GM206 (incl. the GTX 1070) — this is the family that benefits
        # most from GPU mode.
        gpu_encoder="hevc_nvenc",
        gpu_vargs_fn=_hevc_nvenc_args,
        gpu_family="hevc",
        gpu_encoders_by_api={"nvenc": "hevc_nvenc", "qsv": "hevc_qsv",
                             "vaapi": "hevc_vaapi"},
    ),
]

AUDIO_PROFILES: list[AudioProfile] = [
    AudioProfile(label="Opus (96k)",  params=["-c:a", "libopus", "-b:a", "96k"],
                 ffmpeg_encoder_name="libopus", ffprobe_codec_name="opus"),
    AudioProfile(label="Opus (128k)", params=["-c:a", "libopus", "-b:a", "128k"],
                 ffmpeg_encoder_name="libopus", ffprobe_codec_name="opus"),
    AudioProfile(label="Opus (64k)",  params=["-c:a", "libopus", "-b:a", "64k"],
                 ffmpeg_encoder_name="libopus", ffprobe_codec_name="opus"),
    AudioProfile(label="Vorbis (128k)", params=["-c:a", "libvorbis", "-b:a", "128k"],
                 ffmpeg_encoder_name="libvorbis", ffprobe_codec_name="vorbis"),
    AudioProfile(label="Vorbis (192k)", params=["-c:a", "libvorbis", "-b:a", "192k"],
                 ffmpeg_encoder_name="libvorbis", ffprobe_codec_name="vorbis"),
    AudioProfile(label="FLAC (lossless)", params=["-c:a", "flac"],
                 ffmpeg_encoder_name="flac", ffprobe_codec_name="flac"),
    # IAMF — AOMedia Immersive Audio Model and Formats (RFC 9454 family).
    # Built on Opus internally; requires ffmpeg compiled with --enable-libiamf.
    # CANNOT be muxed into MKV/WebM — must use the MP4 container (see below).
    # The -strict experimental flag is harmless on ffmpeg builds where libiamf
    # is already stable, and required on builds where it's still flagged
    # experimental, so we always pass it for forward compatibility.
    AudioProfile(
        label="IAMF (128k)",
        params=["-c:a", "libiamf", "-b:a", "128k", "-strict", "experimental"],
        ffmpeg_encoder_name="libiamf",
        ffprobe_codec_name="iamf",
    ),
]

CONTAINER_PROFILES: list[ContainerProfile] = [
    ContainerProfile(label="MKV (Matroska)", ext="mkv"),
    ContainerProfile(label="WebM",           ext="webm"),
    # MP4 is required for IAMF audio (MKV/WebM cannot mux the IAMF codec).
    # Also useful as a more universally compatible output container.
    ContainerProfile(label="MP4",            ext="mp4"),
]


# ──────────────────────────────────────────────────────────────────────────────
# FFMPEG_LIB_KEY_MAP — single source of truth (OTC-007, SEI CERT MSC04-C).
#
# Maps the `ffmpeg_encoder` field of a VideoCodecProfile (e.g. "libsvtav1",
# "libvpx-vp9") to the corresponding key in EnvProbe.ffmpeg_libs (which is
# populated by _probe_ffmpeg_libs()).
#
# v2 had this map duplicated in three call sites:
#   - _ffmpeg_fallback_encode (around line 2075)
#   - _probe_and_init status bar (around line 4309)
#   - _handle_vs_incompat fallback check (around line 4532)
# Adding a new codec required updating all three in sync — a classic
# MSC04-C violation. v3 hoists it to one module-level constant.
# ──────────────────────────────────────────────────────────────────────────────
FFMPEG_LIB_KEY_MAP: dict[str, str] = {
    "libsvtav1":  "libsvtav1",
    "libaom-av1": "libaom",
    "libvpx-vp9": "libvpx",
    "libx265":    "libx265",
    # v4.6.0: hardware encoders. These keys are populated by
    # _probe_ffmpeg_libs() alongside the software encoders, and — unlike
    # the compiled-in check — EncoderWorker additionally gates the GPU
    # path on env.gpu.functional (a real encode smoke test), because a
    # ffmpeg build can list an NVENC encoder that the installed driver
    # cannot open (NVENC API version mismatch).
    "hevc_nvenc": "hevc_nvenc",
    "h264_nvenc": "h264_nvenc",
    "av1_nvenc":  "av1_nvenc",
    # v4.8.0: hardware APIs for AMD (VAAPI) and Intel (QSV) profiles.
    "hevc_vaapi": "hevc_vaapi",
    "h264_vaapi": "h264_vaapi",
    "av1_vaapi":  "av1_vaapi",
    "vp9_vaapi":  "vp9_vaapi",
    "hevc_qsv":   "hevc_qsv",
    "h264_qsv":   "h264_qsv",
    "av1_qsv":    "av1_qsv",
}


def ffmpeg_lib_key_for(ffmpeg_encoder: str) -> str:
    """Look up the ffmpeg_libs key for a given ffmpeg encoder name.

    Returns the encoder name itself if no mapping is known — this preserves
    forward compatibility with encoders added after this map was last
    updated (the caller's .get() will then return False, which is the
    safe default for an unknown encoder).
    """
    return FFMPEG_LIB_KEY_MAP.get(ffmpeg_encoder, ffmpeg_encoder)


# ── Resolution presets ──
# Aspect ratios:
#   Standard 16:9  -> w/h = 1.778
#   Wide 21:9      -> w/h = 2.333
#   Ultrawide 32:9 -> w/h = 3.556

@dataclass
class ResolutionProfile:
    label: str              # Display label in dropdown, e.g. "1080p Wide (2560x1080)"
    category: str           # Grouping key: "standard", "wide", "ultrawide", "original"
    width: int | None    # None for "original" (no scaling)
    height: int | None   # None for "original"
    aspect_label: str       # "16:9", "21:9", "32:9", "Source"


RESOLUTION_PRESETS: list[ResolutionProfile] = [
    # ── Original (no scaling) ──
    ResolutionProfile("Original (No Scaling)", "original", None, None, "Source"),

    # ── Standard 16:9 ──
    ResolutionProfile("480p  ( 854x 480)",      "standard",   854,  480, "16:9"),
    ResolutionProfile("720p  (1280x 720)",      "standard",  1280,  720, "16:9"),
    ResolutionProfile("1080p (1920x1080)",      "standard",  1920, 1080, "16:9"),
    ResolutionProfile("2K    (2560x1440)",      "standard",  2560, 1440, "16:9"),
    ResolutionProfile("4K    (3840x2160)",      "standard",  3840, 2160, "16:9"),

    # ── Wide 21:9 ──
    ResolutionProfile("480p  Wide  ( 854x 366)",  "wide",   854,  366, "21:9"),
    ResolutionProfile("720p  Wide  (1280x 549)",  "wide",  1280,  549, "21:9"),
    ResolutionProfile("1080p Wide  (2560x1080)", "wide",  2560, 1080, "21:9"),
    ResolutionProfile("2K    Wide  (3440x1440)", "wide",  3440, 1440, "21:9"),
    ResolutionProfile("4K    Wide  (5120x2160)", "wide",  5120, 2160, "21:9"),

    # ── Ultrawide 32:9 ──
    ResolutionProfile("480p  UW    (1706x 480)",  "ultrawide", 1706,  480, "32:9"),
    ResolutionProfile("1080p UW    (3840x1080)", "ultrawide", 3840, 1080, "32:9"),
    ResolutionProfile("2K    UW    (5120x1440)", "ultrawide", 5120, 1440, "32:9"),
    ResolutionProfile("4K    UW    (7680x2160)", "ultrawide", 7680, 2160, "32:9"),
]


SUBTITLE_OPTIONS = [
    ("None",    None),
    ("English", "eng"),
]


DEFAULT_INPUT_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".m4v", ".flv", ".wmv", ".webm", ".mpg", ".mpeg"}
