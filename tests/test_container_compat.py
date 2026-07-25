"""
Container-compatibility rule-table tests for
``OpenCodecMaster._check_combo_compatibility``.

QA finding: OTC-012 (table-driven combo rule evaluation).

The rule table inside ``_check_combo_compatibility`` encodes 5 known
container/codec combinations and their severities:

  1. x265 + WebM                                  -> INCOMPATIBLE
  2. IAMF audio + (MKV|WebM, i.e. non-MP4)        -> INCOMPATIBLE
  3. Vorbis + MP4                                 -> WARNING
  4. FLAC + WebM                                  -> WARNING
  5. VP9 + MP4                                    -> WARNING

(IAMF + MP4 is the OK case for rule 2: not fired, no other rule fires,
empty warnings list.)

The rule table is defined as closures *inside* the method body, so we
can't test it as a free function. Instead we mock the ``OpenCodecMaster``
instance: build it via ``__new__`` (skip the heavy ``__init__`` that
constructs the whole GUI), set ``codec_combo`` / ``audio_combo`` /
``container_combo`` to ``MagicMock`` objects whose ``currentIndex()``
returns the index we want to test, and patch ``_log`` to capture warnings.
Then we call ``_check_combo_compatibility`` directly and assert on the
returned list.

The 6 cases (one per rule plus the IAMF+MP4 happy case) exercise every
predicate in the table.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

# ─────────────────────────────────────────────────────────────────────────────
#  Indices into the module-level VIDEO_CODECS / AUDIO_PROFILES /
#  CONTAINER_PROFILES lists (defined in scripts/open-transcode.py).
# ─────────────────────────────────────────────────────────────────────────────
# VIDEO_CODECS[0] = "AV1 (SVT-AV1)"   — ffmpeg_encoder="libsvtav1"
# VIDEO_CODECS[1] = "VP9"             — ffmpeg_encoder="libvpx-vp9"
# VIDEO_CODECS[2] = "x265 (HEVC)"     — ffmpeg_encoder="libx265"
#
# AUDIO_PROFILES[0] = "Opus (96k)"    — ffmpeg_encoder_name="libopus"
# AUDIO_PROFILES[3] = "Vorbis (128k)" — ffmpeg_encoder_name="libvorbis"
# AUDIO_PROFILES[5] = "FLAC"          — ffmpeg_encoder_name="flac"
# AUDIO_PROFILES[6] = "IAMF (128k)"   — ffmpeg_encoder_name="libiamf"
#
# CONTAINER_PROFILES[0] = "MKV"  — ext="mkv"
# CONTAINER_PROFILES[1] = "WebM" — ext="webm"
# CONTAINER_PROFILES[2] = "MP4"  — ext="mp4"


def _make_master_for_compat(opentranscode_module, codec_idx, audio_idx, container_idx):
    """Build a minimal ``OpenCodecMaster`` for compatibility-rule testing.

    Skips the real ``__init__`` (which constructs the whole QMainWindow UI
    tree) and sets only the three combo-box attributes that
    ``_check_combo_compatibility`` reads. The ``_log`` method is patched to
    capture warnings into ``master._logged`` so the test can also verify
    what was logged (not just what was returned).
    """
    master = opentranscode_module.OpenCodecMaster.__new__(opentranscode_module.OpenCodecMaster)

    codec_combo = MagicMock()
    codec_combo.currentIndex.return_value = codec_idx
    audio_combo = MagicMock()
    audio_combo.currentIndex.return_value = audio_idx
    container_combo = MagicMock()
    container_combo.currentIndex.return_value = container_idx

    master.codec_combo = codec_combo
    master.audio_combo = audio_combo
    master.container_combo = container_combo

    logged: list[str] = []
    master._log = lambda msg: logged.append(msg)
    master._logged = logged
    return master


# ─────────────────────────────────────────────────────────────────────────────
#  Test cases — one per rule in the table, plus the IAMF+MP4 happy case.
# ─────────────────────────────────────────────────────────────────────────────

def test_hevc_in_webm_incompatible(opentranscode_module):
    """Rule 1: x265 + WebM -> INCOMPATIBLE.

    HEVC (x265) cannot be muxed into WebM — the WebM container only
    supports VP8/VP9 video and Opus/Vorbis audio.
    """
    master = _make_master_for_compat(
        opentranscode_module,
        codec_idx=2,      # x265 (HEVC) — ffmpeg_encoder="libx265"
        audio_idx=0,      # Opus 96k (irrelevant for this rule)
        container_idx=1,  # WebM — ext="webm"
    )
    warnings = master._check_combo_compatibility()

    assert any(w.startswith("INCOMPATIBLE:") for w in warnings), (
        f"Expected an INCOMPATIBLE warning for x265+WebM, got: {warnings}"
    )
    hevc_warning = next(w for w in warnings if w.startswith("INCOMPATIBLE:"))
    assert "x265" in hevc_warning or "HEVC" in hevc_warning
    assert "WebM" in hevc_warning


def test_iamf_in_mkv_incompatible(opentranscode_module):
    """Rule 2: IAMF audio + MKV -> INCOMPATIBLE.

    IAMF (AOMedia Immersive Audio) requires the MP4 container — MKV and
    WebM cannot mux the IAMF codec.
    """
    master = _make_master_for_compat(
        opentranscode_module,
        codec_idx=0,      # AV1 (irrelevant for this rule)
        audio_idx=6,      # IAMF — ffmpeg_encoder_name="libiamf"
        container_idx=0,  # MKV — ext="mkv" (non-MP4)
    )
    warnings = master._check_combo_compatibility()

    assert any(w.startswith("INCOMPATIBLE:") for w in warnings), (
        f"Expected an INCOMPATIBLE warning for IAMF+MKV, got: {warnings}"
    )
    iamf_warning = next(w for w in warnings if w.startswith("INCOMPATIBLE:"))
    assert "IAMF" in iamf_warning
    assert "MP4" in iamf_warning  # message tells user to switch to MP4


def test_iamf_in_mp4_ok(opentranscode_module):
    """Rule 2 happy path: IAMF audio + MP4 -> no INCOMPATIBLE.

    With AV1 video + IAMF audio + MP4 container, none of the 5 rules fire
    (the only audio-triggered rule for MP4 is Vorbis-in-MP4; the only
    video-triggered rule for MP4 is VP9-in-MP4; AV1+IAMF+MP4 hits neither).
    The warnings list should be empty.
    """
    master = _make_master_for_compat(
        opentranscode_module,
        codec_idx=0,      # AV1 (not VP9, not x265)
        audio_idx=6,      # IAMF
        container_idx=2,  # MP4 (so _is_iamf_non_mp4 does not fire)
    )
    warnings = master._check_combo_compatibility()

    assert warnings == [], (
        f"Expected no warnings for AV1+IAMF+MP4, got: {warnings}"
    )


def test_vorbis_in_mp4_warning(opentranscode_module):
    """Rule 3: Vorbis + MP4 -> WARNING.

    Vorbis in MP4 has limited player support — it works in some players
    (e.g. VLC) but not in many hardware / mobile players. Opus or
    MKV/WebM is the recommended alternative.
    """
    master = _make_master_for_compat(
        opentranscode_module,
        codec_idx=0,      # AV1 (not VP9, so VP9 rule doesn't fire too)
        audio_idx=3,      # Vorbis — ffmpeg_encoder_name="libvorbis"
        container_idx=2,  # MP4 — ext="mp4"
    )
    warnings = master._check_combo_compatibility()

    assert any(w.startswith("WARNING:") for w in warnings), (
        f"Expected a WARNING for Vorbis+MP4, got: {warnings}"
    )
    assert not any(w.startswith("INCOMPATIBLE:") for w in warnings), (
        f"Vorbis+MP4 is a soft warning, not a hard incompatibility: {warnings}"
    )
    vorbis_warning = next(w for w in warnings if w.startswith("WARNING:"))
    assert "Vorbis" in vorbis_warning


def test_flac_in_webm_warning(opentranscode_module):
    """Rule 4: FLAC + WebM -> WARNING.

    FLAC in WebM is rarely supported by players — MKV is the recommended
    container for FLAC audio.
    """
    master = _make_master_for_compat(
        opentranscode_module,
        codec_idx=0,      # AV1 (not x265, so HEVC rule doesn't fire too)
        audio_idx=5,      # FLAC — ffmpeg_encoder_name="flac"
        container_idx=1,  # WebM — ext="webm"
    )
    warnings = master._check_combo_compatibility()

    assert any(w.startswith("WARNING:") for w in warnings), (
        f"Expected a WARNING for FLAC+WebM, got: {warnings}"
    )
    assert not any(w.startswith("INCOMPATIBLE:") for w in warnings), (
        f"FLAC+WebM is a soft warning, not a hard incompatibility: {warnings}"
    )
    flac_warning = next(w for w in warnings if w.startswith("WARNING:"))
    assert "FLAC" in flac_warning


def test_vp9_in_mp4_warning(opentranscode_module):
    """Rule 5: VP9 + MP4 -> WARNING.

    VP9 in MP4 has uneven player support — WebM is the canonical VP9
    container.
    """
    master = _make_master_for_compat(
        opentranscode_module,
        codec_idx=1,      # VP9 — ffmpeg_encoder="libvpx-vp9"
        audio_idx=0,      # Opus (not Vorbis, so Vorbis rule doesn't fire too)
        container_idx=2,  # MP4 — ext="mp4"
    )
    warnings = master._check_combo_compatibility()

    assert any(w.startswith("WARNING:") for w in warnings), (
        f"Expected a WARNING for VP9+MP4, got: {warnings}"
    )
    assert not any(w.startswith("INCOMPATIBLE:") for w in warnings), (
        f"VP9+MP4 is a soft warning, not a hard incompatibility: {warnings}"
    )
    vp9_warning = next(w for w in warnings if w.startswith("WARNING:"))
    assert "VP9" in vp9_warning
