"""Linux distro detection and per-distro profile registry.

Replaces the v1 250-line if/elif chain with a tuple-of-dataclasses
table (``DISTRO_REGISTRY``). Adding a new distro is a one-row change.
Pure stdlib; no internal package dependencies.
"""

import os
import platform
import time
from dataclasses import dataclass, field
from pathlib import Path

# ──────────────────────────────────────────────
#  DISTRO DETECTION & PROFILES
# ──────────────────────────────────────────────

@dataclass
class DistroProfile:
    family: str              # Canonical family: arch, debian, redhat, suse, nixos, unknown
    name: str                # Pretty name: "Arch Linux", "Fedora 40", etc.
    version_id: str          # e.g. "40", "15.6", "24.05"
    pkg_manager: str         # e.g. "pacman", "dnf", "zypper", "apt", "nix"
    install_cmd_template: str  # e.g. "sudo pacman -S {packages}"
    binary_extra_paths: list[str]  # Distro-specific dirs to search for binaries
    av1an_known_encoder_names: list[str]  # Names this distro's av1an build may accept
    ffmpeg_pkg: str          # Package name providing ffmpeg
    av1an_pkg: str           # Package name providing av1an
    notes: str               # Distro-specific quirks worth showing the user
    # Runtime dependency packages (key = generic name, value = distro package name)
    dep_pkgs: dict[str, str] = field(default_factory=dict)
    # Binaries that av1an invokes directly (not via ffmpeg)
    encoder_binaries: dict[str, list[str]] = field(default_factory=dict)
    # VSScript package name — on most distros this is bundled into 'vapoursynth',
    # but Debian/Ubuntu split it into a separate -script-dev package.
    # If set, this takes priority over dep_pkgs["vapoursynth"] for the VS check.
    vsscript_pkg: str = ""


def _read_os_release() -> dict[str, str]:
    """Parse /etc/os-release into a dict. Falls back to empty dict."""
    os_release = Path("/etc/os-release")
    fallback = Path("/usr/lib/os-release")
    target = os_release if os_release.exists() else fallback
    if not target.exists():
        return {}
    data = {}
    for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            key, _, val = line.partition("=")
            data[key.strip()] = val.strip().strip('"')
    return data


# ──────────────────────────────────────────────────────────────────────────────
# DISTRO_REGISTRY — data-driven distro detection (v3, OTC-014).
#
# v1/v2 had a 250-line if/elif chain in detect_distro() with one branch per
# distro family. Each branch constructed a DistroProfile with mostly-identical
# fields — a classic SEI CERT MSC04-C violation (no single source of truth).
#
# v3 collapses the chain into a tuple-of-dicts table. Each entry has:
#   ids:           tuple of distro_id strings that match this family
#   id_likes:      tuple of ID_LIKE substrings that also match this family
#   family:        canonical family name
#   pkg_manager:   package manager binary name
#   install_cmd:   template with {packages} placeholder
#   extra_paths:   list of distro-specific binary search paths
#   dep_pkgs:      map of generic name -> distro package name
#   notes:         distro-specific quirks string
#   vsscript_pkg:  (optional) separate VSScript package name
#
# Adding a new distro is now a single-table-row change — no code modification.
# The encoder_binaries field is identical across all distros and lives in the
# function body (it's the same dict literal every time).
# ──────────────────────────────────────────────────────────────────────────────

# encoder_binaries is identical for every distro — define once.
_ENCODER_BINARIES: dict[str, list[str]] = {
    "svt_av1": ["SvtAv1EncApp", "svt_av1"],
    "vpx": ["vpxenc"],
    "x265": ["x265"],
}

# Common av1an encoder names known across distros.
_AV1AN_KNOWN_ENCODERS: list[str] = ["svt_av1", "svt", "aom", "rav1e", "vpx", "x265"]


@dataclass(frozen=True)
class _DistroEntry:
    """One row in the DISTRO_REGISTRY table."""
    ids: tuple[str, ...]              # exact distro_id matches
    id_likes: tuple[str, ...]         # ID_LIKE substring matches
    family: str
    pkg_manager: str
    install_cmd: str                  # template with {packages}
    extra_paths: tuple[str, ...]
    dep_pkgs: dict[str, str]
    notes: str
    vsscript_pkg: str = ""


DISTRO_REGISTRY: tuple[_DistroEntry, ...] = (
    _DistroEntry(
        ids=("arch", "manjaro", "endeavouros", "garuda", "cachyos"),
        id_likes=("arch",),
        family="arch",
        pkg_manager="pacman",
        install_cmd="sudo pacman -S {packages}",
        extra_paths=("/usr/bin", "/usr/local/bin", "~/.local/bin", "~/.cargo/bin"),
        dep_pkgs={
            "vapoursynth": "vapoursynth",
            "svt-av1": "svt-av1",
            "x265": "x265",
            "vpx": "libvpx",
            "opus": "libopus",
            "vorbis": "libvorbis",
            "flac": "flac",
        },
        notes=(
            "Arch/Manjaro: av1an is in the AUR (yay -S av1an) or community repo. "
            "SVT-AV1 encoder name is typically 'svt_av1'. "
            "Cargo-installed av1an may live in ~/.cargo/bin."
        ),
    ),
    _DistroEntry(
        ids=("fedora",),
        id_likes=("fedora",),
        family="redhat",
        pkg_manager="dnf",
        install_cmd="sudo dnf install {packages}",
        extra_paths=("/usr/bin", "/usr/local/bin", "~/.cargo/bin"),
        dep_pkgs={
            "vapoursynth": "vapoursynth",
            "svt-av1": "svt-av1",
            "x265": "x265",
            "vpx": "libvpx-tools",
            "opus": "opus",
            "vorbis": "libvorbis",
            "flac": "flac",
        },
        notes=(
            "Fedora: av1an may require COPR enablement first: "
            "sudo dnf copr enable sergiomb/av1an  (or build from source). "
            "SVT-AV1 is in the main repos as 'svt-av1'. "
            "Ensure RPM Fusion is enabled for full codec support."
        ),
    ),
    _DistroEntry(
        ids=("rhel", "centos", "rocky", "almalinux", "ol"),
        id_likes=("rhel", "centos"),
        family="redhat",
        # RHEL-family: dnf if present, fall back to yum
        pkg_manager="",  # resolved at runtime in detect_distro()
        install_cmd="",  # resolved at runtime in detect_distro()
        extra_paths=("/usr/bin", "/usr/local/bin", "~/.cargo/bin"),
        dep_pkgs={
            "vapoursynth": "vapoursynth",
            "svt-av1": "svt-av1",
            "x265": "x265",
            "vpx": "libvpx-tools",
            "opus": "opus",
            "vorbis": "libvorbis",
            "flac": "flac",
        },
        notes=(
            "RHEL/CentOS/Rocky/Alma: av1an is NOT in default repos. "
            "Options: (1) cargo install av1an, (2) build from GitHub source, "
            "(3) use pre-built binary from releases. "
            "Enable EPEL + RPM Fusion for FFmpeg codec support."
        ),
    ),
    _DistroEntry(
        ids=("opensuse-leap", "opensuse-tumbleweed", "sles"),
        id_likes=("suse",),
        family="suse",
        pkg_manager="zypper",
        install_cmd="sudo zypper install {packages}",
        extra_paths=("/usr/bin", "/usr/local/bin", "~/.cargo/bin"),
        dep_pkgs={
            "vapoursynth": "vapoursynth",
            "svt-av1": "svt-av1",
            "x265": "x265",
            "vpx": "libvpx",
            "opus": "libopus",
            "vorbis": "libvorbis",
            "flac": "flac",
        },
        notes=(
            "openSUSE: av1an may be available via OBS (Open Build Service). "
            "Check: https://build.opensuse.org/package/show/multimedia:apps/av1an. "
            "Packman repo provides FFmpeg with full codec support."
        ),
    ),
    _DistroEntry(
        ids=("nixos",),
        id_likes=("nixos",),
        family="nixos",
        pkg_manager="nix",
        install_cmd="nix-shell -p {packages}",
        extra_paths=("/run/current-system/sw/bin", "~/.nix-profile/bin"),
        dep_pkgs={
            "vapoursynth": "vapoursynth",
            "svt-av1": "svt-av1",
            "x265": "x265",
            "vpx": "libvpx",
            "opus": "opus",
            "vorbis": "libvorbis",
            "flac": "flac",
        },
        notes=(
            "NixOS: Use 'nix-shell -p ffmpeg av1an' or add to configuration.nix. "
            "Binaries live under /run/current-system/sw/bin or ~/.nix-profile/bin. "
            "av1an CLI flags may differ from other distros depending on the nixpkgs channel."
        ),
    ),
    _DistroEntry(
        ids=("debian", "ubuntu", "linuxmint", "pop"),
        id_likes=("debian",),
        family="debian",
        pkg_manager="apt",
        install_cmd="sudo apt install {packages}",
        extra_paths=("/usr/bin", "/usr/local/bin", "~/.cargo/bin"),
        dep_pkgs={
            "vapoursynth": "vapoursynth",
            "svt-av1": "svtav1",
            "x265": "x265",
            "vpx": "libvpx-tools",
            "opus": "libopus-dev",
            "vorbis": "libvorbis-dev",
            "flac": "flac",
        },
        notes=(
            "Debian/Ubuntu: av1an is in the repos (apt install av1an). "
            "Debian repo builds may use 'svt' as encoder name instead of 'svt_av1'. "
            "VSScript is in a separate package: libvapoursynth-script-dev. "
            "For newer builds, consider cargo install av1an."
        ),
        vsscript_pkg="libvapoursynth-script-dev",
    ),
)


def _match_distro_entry(distro_id: str, id_like: list[str]) -> _DistroEntry | None:
    """Find the first DISTRO_REGISTRY entry whose ids or id_likes match.

    SEI CERT MSC04-C spirit: the matching logic is one flat loop over a
    table — no nested if/elif chain. Adding a new distro is a one-line
    table change in DISTRO_REGISTRY above; this function never needs
    modification.
    """
    for entry in DISTRO_REGISTRY:
        if distro_id in entry.ids:
            return entry
        if any(like in id_like for like in entry.id_likes):
            return entry
    return None


def detect_distro() -> DistroProfile:
    """
    Detect the running Linux distribution via /etc/os-release.
    Returns a DistroProfile with distro-specific package manager,
    install commands, binary search paths, and known quirks.

    v3 (OTC-014): the per-distro data lives in DISTRO_REGISTRY above.
    This function is now ~30 lines of glue instead of a 250-line
    if/elif chain.
    """
    info = _read_os_release()
    id_like = info.get("ID_LIKE", "").lower().split()
    distro_id = info.get("ID", "").lower()
    pretty = info.get("PRETTY_NAME", info.get("NAME", platform.system()))
    version = info.get("VERSION_ID", "?")

    entry = _match_distro_entry(distro_id, id_like)

    if entry is None:
        # Fallback: unknown distro
        return DistroProfile(
            family="unknown",
            name=pretty,
            version_id=version,
            pkg_manager="unknown",
            install_cmd_template="# Unknown distro — install ffmpeg and av1an manually",
            binary_extra_paths=["/usr/bin", "/usr/local/bin", "~/.cargo/bin", "~/.local/bin"],
            av1an_known_encoder_names=list(_AV1AN_KNOWN_ENCODERS),
            ffmpeg_pkg="ffmpeg",
            av1an_pkg="av1an",
            dep_pkgs={},
            encoder_binaries=dict(_ENCODER_BINARIES),
            notes="Unknown distro detected. Ensure ffmpeg and av1an are in PATH.",
        )

    # Resolve runtime-determined fields (RHEL family: dnf vs yum)
    pkg_manager = entry.pkg_manager
    install_cmd = entry.install_cmd
    if not pkg_manager:
        # RHEL/CentOS family: pick dnf if installed, else yum
        has_dnf = Path("/usr/bin/dnf").exists()
        pkg_manager = "dnf" if has_dnf else "yum"
        install_cmd = (
            "sudo dnf install {packages}" if has_dnf
            else "sudo yum install {packages}"
        )

    return DistroProfile(
        family=entry.family,
        name=pretty,
        version_id=version,
        pkg_manager=pkg_manager,
        install_cmd_template=install_cmd,
        binary_extra_paths=list(entry.extra_paths),
        av1an_known_encoder_names=(
            # Arch family includes the additional 'svt-av1' alias
            ["svt_av1", "svt", "svt-av1", "aom", "rav1e", "vpx", "x265"]
            if entry.family == "arch"
            else list(_AV1AN_KNOWN_ENCODERS)
        ),
        ffmpeg_pkg="ffmpeg",
        av1an_pkg="av1an",
        dep_pkgs=dict(entry.dep_pkgs),
        encoder_binaries=dict(_ENCODER_BINARIES),
        vsscript_pkg=entry.vsscript_pkg,
        notes=entry.notes,
    )

