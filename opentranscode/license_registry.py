"""License notice registry for third-party components.

Holds the canonical ``LicenseNotice`` table + helpers that filter the
notices down to the ones active in the running environment. Pure data
+ pure functions; the ``env`` parameter is duck-typed so this module
does not import ``EnvProbe`` (avoids a circular dependency).
"""

from dataclasses import dataclass

# ──────────────────────────────────────────────
#  LICENSE NOTICES — third-party components invoked by this application.
#
#  Each entry is a tuple of (tool name, SPDX identifier, short attribution,
#  full notice). The short form is used for the startup banner and the
#  pre-transcode summary; the full form is shown in the About dialog.
#
#  This application is a thin orchestration layer; it does not incorporate
#  the source code of any of these tools. The license obligations of each
#  tool therefore flow through to the end user independently, and this
#  registry exists to make those obligations visible at runtime.
# ──────────────────────────────────────────────

@dataclass(frozen=True)
class LicenseNotice:
    """Immutable descriptor for a third-party component license.

    SEI CERT MSC04-C spirit: secrets and licensing data are not duplicated
    across the codebase; the canonical source is this table.
    """
    name: str            # e.g. "FFmpeg"
    spdx: str            # e.g. "LGPL-2.1-or-later"
    home_url: str        # canonical upstream URL
    short: str           # one-line attribution shown in banners
    full: str            # multi-line notice shown in About dialog


LICENSE_NOTICES: tuple[LicenseNotice, ...] = (
    LicenseNotice(
        name="FFmpeg",
        spdx="LGPL-2.1-or-later (or GPL-2.0-or-later with --enable-gpl)",
        home_url="https://ffmpeg.org",
        short="FFmpeg (LGPL-2.1+, GPL build flags noted at runtime)",
        full=(
            "FFmpeg\n"
            "Copyright (c) FFmpeg developers\n"
            "Licensed under LGPL-2.1-or-later; the build's effective license\n"
            "may upgrade to GPL-2.0-or-later when --enable-gpl or any GPL-only\n"
            "library (libx264, libx265, libfdk-aac) is configured in.\n"
            "Source: https://ffmpeg.org\n"
            "License: https://www.gnu.org/licenses/old-licenses/lgpl-2.1.html"
        ),
    ),
    LicenseNotice(
        name="av1an",
        spdx="GPL-3.0-or-later",
        home_url="https://github.com/master-of-zen/av1an",
        short="av1an (GPL-3.0+)",
        full=(
            "av1an — Av1an is a frame-parallel AV1/VP9/x265 encoder\n"
            "Copyright (c) master-of-zen and contributors\n"
            "Licensed under GPL-3.0-or-later.\n"
            "Source: https://github.com/master-of-zen/av1an\n"
            "License: https://www.gnu.org/licenses/gpl-3.0.html"
        ),
    ),
    LicenseNotice(
        name="VapourSynth",
        spdx="LGPL-2.1-or-later",
        home_url="https://www.vapoursynth.com",
        short="VapourSynth (LGPL-2.1+)",
        full=(
            "VapourSynth — a video processing framework\n"
            "Copyright (c) Fredrik Mellbin and contributors\n"
            "Licensed under LGPL-2.1-or-later.\n"
            "Source: https://github.com/vapoursynth/vapoursynth\n"
            "License: https://www.gnu.org/licenses/old-licenses/lgpl-2.1.html"
        ),
    ),
    LicenseNotice(
        name="SVT-AV1",
        spdx="BSD-3-Clause AND PMK-2-Clause",
        home_url="https://gitlab.com/AOMediaCodec/SVT-AV1",
        short="SVT-AV1 (BSD-3-Clause, AOMedia)",
        full=(
            "SVT-AV1 — Scalable Video Technology for AV1\n"
            "Copyright (c) Alliance for Open Media and contributors\n"
            "Licensed under BSD-3-Clause and the AOMedia Patent License.\n"
            "Source: https://gitlab.com/AOMediaCodec/SVT-AV1\n"
            "License: https://opensource.org/license/bsd-3-clause"
        ),
    ),
    LicenseNotice(
        name="libvpx",
        spdx="BSD-3-Clause",
        home_url="https://github.com/webmproject/libvpx",
        short="libvpx / VP9 (BSD-3-Clause)",
        full=(
            "libvpx — VP8/VP9 codec library\n"
            "Copyright (c) The WebM Project authors\n"
            "Licensed under BSD-3-Clause.\n"
            "Source: https://github.com/webmproject/libvpx\n"
            "License: https://opensource.org/license/bsd-3-clause"
        ),
    ),
    LicenseNotice(
        name="x265",
        spdx="GPL-2.0-or-later (commercial license available)",
        home_url="https://bitbucket.org/multicoreware/x265_git",
        short="x265 / HEVC (GPL-2.0+)",
        full=(
            "x265 — HEVC encoder\n"
            "Copyright (c) MulticoreWare, Inc and contributors\n"
            "Licensed under GPL-2.0-or-later; a commercial license is\n"
            "available from MulticoreWare for non-GPL distribution.\n"
            "Source: https://bitbucket.org/multicoreware/x265_git\n"
            "License: https://www.gnu.org/licenses/old-licenses/gpl-2.0.html"
        ),
    ),
    LicenseNotice(
        name="libopus",
        spdx="BSD-3-Clause",
        home_url="https://opus-codec.org",
        short="libopus / Opus (BSD-3-Clause)",
        full=(
            "libopus — Opus audio codec (IETF RFC 6716)\n"
            "Copyright (c) Xiph.Org Foundation, Skype Limited, Mozilla,\n"
            "and contributors\n"
            "Licensed under BSD-3-Clause.\n"
            "Source: https://github.com/xiph/opus\n"
            "License: https://opensource.org/license/bsd-3-clause"
        ),
    ),
    LicenseNotice(
        name="libvorbis",
        spdx="BSD-3-Clause",
        home_url="https://xiph.org/vorbis",
        short="libvorbis / Vorbis (BSD-3-Clause)",
        full=(
            "libvorbis — Vorbis audio codec\n"
            "Copyright (c) Xiph.Org Foundation and contributors\n"
            "Licensed under BSD-3-Clause.\n"
            "Source: https://github.com/xiph/vorbis\n"
            "License: https://opensource.org/license/bsd-3-clause"
        ),
    ),
    LicenseNotice(
        name="libFLAC",
        spdx="BSD-3-Clause",
        home_url="https://xiph.org/flac",
        short="libFLAC / FLAC (BSD-3-Clause)",
        full=(
            "libFLAC — Free Lossless Audio Codec\n"
            "Copyright (c) Xiph.Org Foundation and contributors\n"
            "Licensed under BSD-3-Clause.\n"
            "Source: https://github.com/xiph/flac\n"
            "License: https://opensource.org/license/bsd-3-clause"
        ),
    ),
    LicenseNotice(
        name="libiamf",
        spdx="BSD-2-Clause",
        home_url="https://github.com/AOMediaCodec/libiamf",
        short="libiamf / IAMF (BSD-2-Clause, AOMedia)",
        full=(
            "libiamf — AOMedia Immersive Audio Model and Formats\n"
            "Copyright (c) Alliance for Open Media and contributors\n"
            "Licensed under BSD-2-Clause.\n"
            "Source: https://github.com/AOMediaCodec/libiamf\n"
            "License: https://opensource.org/license/bsd-2-clause"
        ),
    ),
    LicenseNotice(
        name="Qt / PySide6",
        spdx="LGPL-3.0-only (commercial available from The Qt Company)",
        home_url="https://www.qt.io",
        short="Qt / PySide6 (LGPL-3.0)",
        full=(
            "Qt — application framework\n"
            "Copyright (c) The Qt Company Ltd and contributors\n"
            "Licensed under LGPL-3.0-only; a commercial license is available.\n"
            "Source: https://www.qt.io\n"
            "License: https://www.gnu.org/licenses/lgpl-3.0.html"
        ),
    ),
    LicenseNotice(
        name="Python",
        spdx="PSF-2.0",
        home_url="https://www.python.org",
        short="Python (PSF License)",
        full=(
            "Python — programming language\n"
            "Copyright (c) Python Software Foundation\n"
            "Licensed under the PSF License Agreement.\n"
            "Source: https://www.python.org\n"
            "License: https://docs.python.org/3/license.html"
        ),
    ),
)


def active_license_notices(env) -> list[LicenseNotice]:
    """Return the subset of LICENSE_NOTICES that apply to the running
    environment. Determined by which tools / libraries env reports as
    present. Always includes FFmpeg, Python, and Qt (framework deps).

    Data-driven dispatch: avoids a per-tool if/elif chain by looking up
    each notice's presence in env attributes via a small table.
    """
    presence_rules: tuple[tuple[str, bool], ...] = (
        ("FFmpeg",        bool(getattr(env, "ffmpeg_path", None))),
        ("av1an",         bool(getattr(env, "av1an_path", None))),
        ("VapourSynth",   bool(getattr(env, "vs_version", None))),
        ("SVT-AV1",       bool(getattr(env, "av1an_flags", {}).get("svt_name"))),
        ("libvpx",        bool(getattr(env, "ffmpeg_libs", {}).get("libvpx"))),
        ("x265",          bool(getattr(env, "ffmpeg_libs", {}).get("libx265"))),
        ("libopus",       bool(getattr(env, "ffmpeg_libs", {}).get("libopus"))),
        ("libvorbis",     bool(getattr(env, "ffmpeg_libs", {}).get("libvorbis"))),
        ("libFLAC",       bool(getattr(env, "ffmpeg_libs", {}).get("flac"))),
        ("libiamf",       bool(getattr(env, "ffmpeg_libs", {}).get("libiamf"))),
        ("Qt / PySide6",  True),   # framework, always present
        ("Python",        True),
    )
    active_names = {name for name, present in presence_rules if present}
    return [n for n in LICENSE_NOTICES if n.name in active_names]


def license_banner_short(notices: list[LicenseNotice]) -> str:
    """One-line summary suitable for a status bar or log header."""
    return " | ".join(n.short for n in notices)


def license_banner_full(notices: list[LicenseNotice]) -> str:
    """Multi-line text block suitable for an About / Licenses dialog."""
    sep = "─" * 60
    blocks = [sep, "  OPEN SOURCE LICENSE ATTRIBUTIONS", sep]
    for n in notices:
        blocks.append(n.full)
        blocks.append(sep)
    blocks.append(
        "This application invokes these tools as external processes.\n"
        "Source code of each tool is NOT bundled with this application.\n"
        "For the full text of each license, follow the upstream URL cited\n"
        "above. Questions about redistribution rights should be directed\n"
        "to the upstream projects."
    )
    return "\n".join(blocks)

