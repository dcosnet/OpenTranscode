#!/usr/bin/env python3
"""
OpenTranscode — open-source batch video transcoder (av1an + ffmpeg)
====================================================================
A PySide6 GUI application that orchestrates av1an + ffmpeg for batch video
transcoding. Distro-aware, config-driven (codec / audio / container /
resolution profiles), with a QThread-based encoder worker, a from-git
source builder for resolving VapourSynth / av1an ABI mismatches, and a
retro-futuristic media-console UI.

This launcher script is preserved alongside the ``opentranscode/`` package
for backwards compatibility and as the test target for the mocked test
suite. It mirrors the package's behavior via inline copies of the same
modules. New code should ``import opentranscode`` (the package) instead.

v4.0.0 — Production Release
---------------------------
Resolved the "works up until near the end, never saves chunks into a full
file" bug that affected phone-recorded MP4s with sparse keyframes.

Root cause: when no VapourSynth source plugins are installed (the common
case), av1an auto-selects the Hybrid chunk method, which does
``ffmpeg -c copy -f segment`` to split the source at scene boundaries,
then re-decodes each segment to y4m. Phone-recorded MP4s only have
I-frames every 5-10s, so scene boundaries rarely align with keyframes →
segments start mid-GOP → the decoder errors with "error while decoding
MB 35 25" → the y4m pipe breaks → the encoder fails with "Failed to
read y4m frame delimiter. Read broken. EOF: 1" → every chunk fails →
no concat → no output file.

The fix has three parts:

  1. ``_encode_one`` now accepts a ``chunk_method`` parameter. When av1an
     fails with the y4m break pattern, it recursively retries with
     ``--chunk-method select`` (VapourSynth's select() filter, which
     extracts frames one-by-one and avoids the keyframe-alignment issue).
     This is faster than the ffmpeg fallback (chunk-parallel still works)
     and produces identical-quality output.

  2. The working chunk_method is cached in
     ``env.av1an_flags["chunk_method_override"]`` so subsequent files skip
     the wasted first attempt.

  3. ``env_probe`` now calls ``_probe_vs_source_plugins()`` to detect
     installed VapourSynth source plugins (lsmash, ffms2, bestsource,
     dgdecnv, vszip). When NONE are found, it pre-sets
     ``chunk_method_override = "select"`` to avoid the wasted first
     attempt entirely.

Also fixed: the "SUMMARY block + non-zero exit" diagnostic previously
misdiagnosed y4m break failures as "concat failure" (because SVT-AV1
prints a SUMMARY block per-chunk before the pipe breaks). The check is
now guarded by ``"Failed to read y4m frame delimiter" not in stderr_full``
so it only fires for true concat failures.

New CLI flag: ``--chunk-method {auto,select,hybrid,segment,ffms2,lsmash,
bestsource,dgdecnv}`` lets the user force a specific chunk method.

New e2e test: ``test_y4m_break_recovery_produces_valid_output`` in
test_e2e_real_encode.py — generates a real video, simulates the y4m
break, and verifies the retry-with-select produces a valid AV1/MKV file.

Previous releases
-----------------
v3.x was the production-readiness pass: split the 515-line ``run()`` into
5 single-responsibility methods, narrowed 24 bare ``except Exception``
clauses, added the STOP-button interrupt (SIGTERM/SIGKILL on the process
group), per-worker temp subdirs (race-condition fix), the per-file
av1an→ffmpeg fallback, and the av1an VSScript smoke test. v2.x added
ffprobe pre-validation and the pattern-table failure diagnostics.

Key features
------------
  - Config-driven codec/audio/container profiles (no nested if/else chains)
  - Distro-aware: Arch, Fedora, RHEL/CentOS/Rocky/Alma, openSUSE, NixOS, Debian/Ubuntu
  - Per-distro binary search paths, package manager, install hints, encoder name quirks
  - FFmpeg encoder library availability probe (greys out unavailable codecs in UI)
  - ffprobe pre-validation before encoding
  - QThread worker (thread-safe UI, proper signal/slot)
  - Runtime av1an flag + version probing
  - Dynamic worker count (os.cpu_count - 2)
  - pathlib throughout
  - Per-file progress tracking
  - Container format selection (MKV / WebM / MP4)
  - AV1/VP9/x265 preset selection
  - Configurable input extensions
  - Batch delete with summary prompt (not per-file)
"""

import hashlib
import io
import json
import math
import os
import platform
import re
import shutil
import signal
import subprocess
import site
import sys
import tempfile
import threading
import time
import ctypes
from collections.abc import Callable
from dataclasses import dataclass, field, field
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QCheckBox,
    QTextEdit, QFileDialog, QGroupBox, QStatusBar, QMessageBox,
    QStyleFactory,
)
from PySide6.QtCore import Qt, QThread, Signal, Slot, QPointF, QRectF, QTimer
from PySide6.QtGui import (
    QFont, QPalette, QColor, QPainter, QPen, QBrush,
    QRadialGradient, QFontMetrics,
)


# ──────────────────────────────────────────────
#  RADIO KNOB WIDGET (oldschool rotary control)
# ──────────────────────────────────────────────

class RadioKnob(QWidget):
    """
    A retro radio-style rotary knob widget.
    Supports arc range, tick marks, and a glowing indicator dot.

    Rotation: 7 o'clock (min) to 5 o'clock (max) = 300 degrees.
    """
    valueChanged = Signal(float)

    def __init__(
        self,
        parent=None,
        min_val: float = 0.0,
        max_val: float = 100.0,
        default_val: float = 50.0,
        label: str = "",
        unit: str = "",
        color: tuple = (42, 130, 218),
        num_ticks: int = 17,
        tick_labels: list[str] | None = None,
        snap_ticks: bool = False,
        compact: bool = False,
    ):
        super().__init__(parent)
        self.min_val = min_val
        self.max_val = max_val
        self._value = default_val
        self.label = label
        self.unit = unit
        self.color = QColor(*color)
        self.num_ticks = num_ticks
        self.tick_labels = tick_labels
        self.snap_ticks = snap_ticks
        self._dragging = False
        self.compact = compact

        # Arc geometry: 300-degree sweep, centered at 12 o'clock
        self._arc_start = 210.0   # degrees (7 o'clock)
        self._arc_span = -300.0   # negative = clockwise

        # Scaling factor for compact mode (~70% of full size)
        s = 0.70 if compact else 1.0
        self._s = s
        self.setFixedSize(int(180 * s), int(210 * s))
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    # --- Public API ---

    def value(self) -> float:
        return self._value

    def setValue(self, v: float):
        v = max(self.min_val, min(self.max_val, v))
        if self.snap_ticks:
            v = self._snap(v)
        if v != self._value:
            self._value = v
            self.update()
            self.valueChanged.emit(v)

    def intValue(self) -> int:
        return int(round(self._value))

    def _snap(self, v: float) -> float:
        """Snap to nearest tick."""
        step = (self.max_val - self.min_val) / max(1, self.num_ticks - 1)
        return round((v - self.min_val) / step) * step + self.min_val

    def _val_to_angle(self, v: float) -> float:
        """Map value to angle in degrees (matching the conical gradient)."""
        ratio = (v - self.min_val) / (self.max_val - self.min_val) if self.max_val != self.min_val else 0
        return self._arc_start + ratio * self._arc_span  # goes from 210 -> -90

    def _angle_to_val(self, angle_deg: float) -> float:
        """Map angle back to value."""
        # Normalize angle relative to arc start
        ratio = (angle_deg - self._arc_start) / self._arc_span
        ratio = max(0.0, min(1.0, ratio))
        v = self.min_val + ratio * (self.max_val - self.min_val)
        if self.snap_ticks:
            v = self._snap(v)
        return v

    # --- Painting ---

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        s = self._s  # scale factor (0.7 for compact, 1.0 for full)

        cx = w / 2
        cy = h / 2 - 4 * s
        outer_r = 70 * s
        knob_r = 40 * s
        arc_w = max(1, int(8 * s))
        tick_w = max(1, 1.5 * s)
        bezel_pad = 6 * s

        # --- Outer bezel ring ---
        bezel_grad = QRadialGradient(cx, cy, outer_r + bezel_pad)
        bezel_grad.setColorAt(0.85, QColor(48, 48, 52))
        bezel_grad.setColorAt(1.0, QColor(26, 26, 30))
        p.setBrush(QBrush(bezel_grad))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx, cy), outer_r + bezel_pad, outer_r + bezel_pad)

        # --- Inactive arc (dark track) ---
        p.setPen(QPen(QColor(50, 50, 56), arc_w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(QRectF(cx - outer_r, cy - outer_r, outer_r * 2, outer_r * 2),
                  int(self._arc_start * 16), int(self._arc_span * 16))

        # --- Active arc (colored fill up to current value) ---
        val_angle = self._val_to_angle(self._value)
        active_span = val_angle - self._arc_start
        if abs(active_span) > 0.5:
            arc_color = QColor(self.color)
            p.setPen(QPen(arc_color, arc_w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawArc(QRectF(cx - outer_r, cy - outer_r, outer_r * 2, outer_r * 2),
                      int(self._arc_start * 16), int(active_span * 16))

        # --- Tick marks ---
        for i in range(self.num_ticks):
            t = i / (self.num_ticks - 1) if self.num_ticks > 1 else 0
            tick_angle = self._val_to_angle(self.min_val + t * (self.max_val - self.min_val))
            tick_rad = tick_angle * math.pi / 180.0
            ox = cx + (outer_r + 12 * s) * (-1) * math.sin(tick_rad)
            oy = cy + (outer_r + 12 * s) * (-1) * (-math.cos(tick_rad))
            ix_ = cx + (outer_r + 3 * s) * (-1) * math.sin(tick_rad)
            iy_ = cy + (outer_r + 3 * s) * (-1) * (-math.cos(tick_rad))
            p.setPen(QPen(QColor(130, 130, 130), tick_w))
            p.drawLine(QPointF(ix_, iy_), QPointF(ox, oy))

        # Tick labels (if provided)
        if self.tick_labels:
            p.setFont(QFont("Sans", max(5, int(7 * s))))
            p.setPen(QColor(160, 160, 160))
            step = max(1, self.num_ticks // len(self.tick_labels))
            label_idx = 0
            for i in range(0, self.num_ticks, step):
                if label_idx >= len(self.tick_labels):
                    break
                t = i / (self.num_ticks - 1) if self.num_ticks > 1 else 0
                tick_angle = self._val_to_angle(self.min_val + t * (self.max_val - self.min_val))
                tick_rad = tick_angle * math.pi / 180.0
                lx = cx + (outer_r + 24 * s) * (-1) * math.sin(tick_rad)
                ly = cy + (outer_r + 24 * s) * (-1) * (-math.cos(tick_rad))
                txt = self.tick_labels[label_idx]
                fm = QFontMetrics(p.font())
                tw = fm.horizontalAdvance(txt)
                p.drawText(QPointF(lx - tw / 2, ly + 2 * s), txt)
                label_idx += 1

        # --- Knob body (dark brushed aluminum) ---
        knob_grad = QRadialGradient(cx - 6 * s, cy - 6 * s, knob_r * 1.3)
        knob_grad.setColorAt(0.0, QColor(72, 72, 78))
        knob_grad.setColorAt(0.5, QColor(50, 50, 55))
        knob_grad.setColorAt(1.0, QColor(34, 34, 38))
        p.setBrush(QBrush(knob_grad))
        p.setPen(QPen(QColor(26, 26, 30), max(1, 1.5 * s)))
        p.drawEllipse(QPointF(cx, cy), knob_r, knob_r)

        # --- Inner shadow ring ---
        inner_shadow = QRadialGradient(cx, cy, knob_r - 2)
        inner_shadow.setColorAt(0.85, QColor(0, 0, 0, 0))
        inner_shadow.setColorAt(1.0, QColor(0, 0, 0, 60))
        p.setBrush(QBrush(inner_shadow))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx, cy), knob_r - 1, knob_r - 1)

        # --- Indicator line (pointer) ---
        ptr_angle = self._val_to_angle(self._value)
        ptr_rad = ptr_angle * 3.14159265 / 180.0
        ptr_len = knob_r - 8 * s
        px = cx + ptr_len * (-1) * math.sin(ptr_rad)
        py = cy + ptr_len * (-1) * (-math.cos(ptr_rad))
        p.setPen(QPen(QColor(255, 255, 255, 220), max(1, 2.5 * s),
                     Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(cx, cy), QPointF(px, py))

        # --- Center cap dot ---
        cap_r = max(2, 5 * s)
        cap_grad = QRadialGradient(cx, cy, cap_r)
        cap_grad.setColorAt(0.0, QColor(60, 60, 65))
        cap_grad.setColorAt(1.0, QColor(30, 30, 34))
        p.setBrush(QBrush(cap_grad))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx, cy), cap_r, cap_r)

        # --- Glow dot at arc tip ---
        glow_r = max(3, 10 * s)
        glow_x = cx + outer_r * (-1) * math.sin(ptr_rad)
        glow_y = cy + outer_r * (-1) * (-math.cos(ptr_rad))
        glow = QRadialGradient(glow_x, glow_y, glow_r * 1.2)
        glow.setColorAt(0.0, QColor(self.color.red(), self.color.green(), self.color.blue(), 200))
        glow.setColorAt(1.0, QColor(self.color.red(), self.color.green(), self.color.blue(), 0))
        p.setBrush(QBrush(glow))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(glow_x, glow_y), glow_r, glow_r)

        p.end()

        # --- Label + value text below knob ---
        p2 = QPainter(self)
        p2.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Value line (e.g. "32.0 CRF")
        val_font_sz = max(6, int(13 * s))
        p2.setFont(QFont("Consolas", val_font_sz, QFont.Weight.Bold))
        val_color = QColor(self.color.red(), self.color.green(), self.color.blue())
        p2.setPen(val_color)
        val_text = f"{self._value:.0f} {self.unit}" if self.unit else f"{self._value:.0f}"
        p2.drawText(QRectF(0, h - 38 * s, w, 20 * s), Qt.AlignmentFlag.AlignCenter, val_text)

        # Label line (e.g. "Quality")
        lbl_font_sz = max(5, int(9 * s))
        p2.setFont(QFont("Consolas", lbl_font_sz, QFont.Weight.Bold))
        p2.setPen(QColor(160, 160, 160))
        p2.drawText(QRectF(0, h - 18 * s, w, 16 * s), Qt.AlignmentFlag.AlignCenter, self.label)
        p2.end()

    # --- Input handling ---

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._update_from_mouse(event.position())

    def mouseMoveEvent(self, event):
        if self._dragging:
            self._update_from_mouse(event.position())

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        step = (self.max_val - self.min_val) / max(1, self.num_ticks - 1)
        if delta > 0:
            self.setValue(self._value + step)
        elif delta < 0:
            self.setValue(self._value - step)

    def _update_from_mouse(self, pos: QPointF):
        cx = self.width() / 2
        cy = self.height() / 2 - 4 * self._s
        dx = pos.x() - cx
        dy = pos.y() - cy
        angle = math.degrees(math.atan2(dx, -dy))  # 0=north, CW positive
        if angle < 0:
            angle += 360
        # Clamp to arc range: 210..510 (which is 210..360 and 0..150)
        # Our arc: 210 degrees to -90 (=270) degrees clockwise
        if angle < 210 and angle > 150:
            # Dead zone at bottom (between 150 and 210)
            # Push to nearest end
            angle = 210 if abs(angle - 210) < abs(angle - 510) else 510
        if angle > 360:
            angle -= 360  # normalize back to 0..360
        self.setValue(self._angle_to_val(angle))


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
    # appear here. Thread capping lives in ffmpeg_vargs_fn and in
    # EncoderWorker's --workers.
    params_fn: Callable[[int, int], str]          # (crf, preset) -> av1an video-params string
    ffmpeg_vargs_fn: Callable[[int, int], list[str]]  # (crf, preset) -> ffmpeg -c:v args
    presets: list[str]           # Human-readable preset labels
    preset_map: dict[str, int]   # label -> internal preset value
    # v4.3.0: the codec_name ffprobe returns for files encoded with this
    # profile. Used by _output_already_encoded() to detect skip-existing.
    # av1 → "av1", vp9 → "vp9", hevc → "hevc".
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


# ──────────────────────────────────────────────
#  GPU PROBE (v4.6.0 — NVENC hardware encoding)
# ──────────────────────────────────────────────

# NVENC encoders we know how to drive, in preference order (best
# compression efficiency first). av1_nvenc only exists on RTX 40+; the
# functional smoke test below decides what is actually usable.
_NVENC_ENCODER_NAMES: tuple[str, ...] = ("av1_nvenc", "hevc_nvenc", "h264_nvenc")


@dataclass
class GpuInfo:
    """Result of the GPU/NVENC probe.

    ``encoders``   — encoder name → ffmpeg was BUILT with it (from
                     ``ffmpeg -encoders``).
    ``functional`` — encoder name → a real 0.2s NVENC encode SUCCEEDED.
                     This is the gate EncoderWorker uses: a ffmpeg build
                     can list hevc_nvenc while the installed driver is
                     too old for the NVENC API version it was compiled
                     against ("Driver does not support the required
                     nvenc API version") — only a live encode reveals
                     that.
    ``details``    — encoder name → first stderr line when the smoke
                     test failed (actionable diagnostics).
    """
    name: str = ""                                            # GPU model name via nvidia-smi, "" if unknown
    encoders: dict[str, bool] = field(default_factory=dict)
    functional: dict[str, bool] = field(default_factory=dict)
    details: dict[str, str] = field(default_factory=dict)

    @property
    def usable_encoders(self) -> list[str]:
        """Encoders that passed the live encode test, preference order."""
        return [e for e in _NVENC_ENCODER_NAMES if self.functional.get(e, False)]

    @property
    def has_gpu(self) -> bool:
        return bool(self.usable_encoders)

    @property
    def first_failure_detail(self) -> str:
        """First non-empty failure detail (for user-facing warnings)."""
        for e in _NVENC_ENCODER_NAMES:
            d = self.details.get(e, "")
            if d:
                return d
        return ""


def _probe_gpu(ffmpeg_bin: str) -> GpuInfo:
    """Detect NVIDIA NVENC hardware encoders and verify they actually work.

    Two-stage probe:

      1. Compiled-in check — grep ``ffmpeg -encoders`` for the NVENC
         encoder names. Cheap; answers "could this ffmpeg ever do NVENC".
      2. Functional smoke test — for each compiled-in encoder, encode a
         0.2s 256x264 lavfi color source with ``-c:v <enc> -f null -``.
         Catches the real-world failure modes the compiled-in check
         cannot: NVIDIA driver too old for the ffmpeg build's NVENC API
         version, no /dev/nvidia* access, driver loaded but GPU dead.

    GPU model name is best-effort via nvidia-smi (display only).
    """
    info = GpuInfo()

    if not ffmpeg_bin:
        return info

    # --- Stage 1: compiled-in encoders ---
    try:
        res = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10,
        )
        encoders_out = res.stdout or ""
    except (OSError, subprocess.SubprocessError):
        encoders_out = ""

    for enc in _NVENC_ENCODER_NAMES:
        info.encoders[enc] = f" {enc} " in encoders_out

    compiled_in = [e for e in _NVENC_ENCODER_NAMES if info.encoders[e]]
    if not compiled_in:
        return info  # no hardware encoders in this build — skip stage 2

    # --- GPU model name (display only, never gates anything) ---
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            res = subprocess.run(
                [nvidia_smi, "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5,
            )
            if res.returncode == 0 and res.stdout.strip():
                info.name = res.stdout.strip().splitlines()[0].strip()
        except (OSError, subprocess.SubprocessError):
            pass

    # --- Stage 2: functional smoke test per compiled-in encoder ---
    for enc in compiled_in:
        try:
            res = subprocess.run(
                [
                    ffmpeg_bin, "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi",
                    "-i", "color=c=black:s=256x256:d=0.2:r=24",
                    "-frames:v", "5",
                    "-c:v", enc, "-f", "null", "-",
                ],
                capture_output=True, text=True, timeout=20,
            )
            info.functional[enc] = res.returncode == 0
            if res.returncode != 0:
                # First stderr line with substance (nvenc errors are
                # prefixed "hevc_nvenc @ 0x...]" — keep them readable).
                for line in (res.stderr or "").splitlines():
                    line = line.strip()
                    if line:
                        # Strip the "name @ 0xADDR]" prefix for brevity.
                        line = re.sub(r"^\[[^]]+@\s*0x[0-9a-f]+\]\s*", "", line)
                        info.details[enc] = line[:160]
                        break
        except subprocess.TimeoutExpired:
            info.functional[enc] = False
            info.details[enc] = f"{enc} smoke test timed out after 20s"
        except (OSError, subprocess.SubprocessError) as e:
            info.functional[enc] = False
            info.details[enc] = str(e)[:160]

    return info


# ──────────────────────────────────────────────
#  ENVIRONMENT PROBE (distro-aware, extended)
# ──────────────────────────────────────────────

@dataclass
class EnvProbe:
    distro: DistroProfile = field(default_factory=lambda: DistroProfile(
        family="unknown", name="Unknown", version_id="?",
        pkg_manager="unknown", install_cmd_template="",
        binary_extra_paths=[], av1an_known_encoder_names=[],
        ffmpeg_pkg="ffmpeg", av1an_pkg="av1an", notes=""
    ))
    av1an_path: str | None = None
    ffmpeg_path: str | None = None
    ffprobe_path: str | None = None
    # v3 (OTC-011, PEP 868): parameterized dict/list type hints.
    # av1an_flags values are sometimes str (flag name), sometimes bool
    # (has_chunk_method), sometimes int — keep as dict[str, object] for honesty.
    av1an_flags: dict[str, object] = field(default_factory=dict)
    av1an_version: str | None = None
    ffmpeg_version: str | None = None
    ffmpeg_libs: dict[str, bool] = field(default_factory=dict)  # lib name -> available
    runtime_deps: dict[str, bool] = field(default_factory=dict)  # dep name -> present
    missing_dep_pkgs: list[str] = field(default_factory=list)  # distro pkg names to install
    vs_version: str | None = None  # VapourSynth version string (for diagnostics)
    vs_script_lib: str | None = None  # path to libvapoursynth-script.so that passed
    cpu: CpuTopology = field(default_factory=lambda: CpuTopology(1, 1, 1, "Unknown"))
    # v4.6.0: GPU/NVENC probe result. EncoderWorker reads
    # env.gpu.functional[encoder_name] when the engine is auto/gpu.
    gpu: GpuInfo = field(default_factory=GpuInfo)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return (self.av1an_path is not None and self.ffmpeg_path is not None
                and not self.errors and not self.missing_dep_pkgs)

    @property
    def dep_install_hint(self) -> str:
        """Generate a distro-specific install command for missing runtime deps."""
        if not self.missing_dep_pkgs or self.distro.family == "unknown":
            return ""
        return self.distro.install_cmd_template.format(packages=" ".join(self.missing_dep_pkgs))

    @property
    def install_hint(self) -> str:
        """Generate a distro-specific install command for missing packages."""
        missing = []
        if self.av1an_path is None:
            missing.append(self.distro.av1an_pkg)
        if self.ffmpeg_path is None:
            missing.append(self.distro.ffmpeg_pkg)
        if not missing:
            return ""
        return self.distro.install_cmd_template.format(packages=" ".join(missing))


def _find_binary(name: str, distro: DistroProfile) -> str | None:
    """
    Search for a binary in: (1) standard PATH via shutil.which, then
    (2) distro-specific extra paths (expanded ~). Returns first match.
    """
    # Standard PATH search
    found = shutil.which(name)
    if found:
        return found

    # Distro-specific extra paths
    for raw_path in distro.binary_extra_paths:
        expanded = Path(raw_path).expanduser()
        candidate = expanded / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    return None


def _probe_ffmpeg_libs(ffmpeg_bin: str) -> dict[str, bool]:
    """Check which encoder/decoder libraries ffmpeg was compiled with.
    Runs ffmpeg -encoders ONCE and greps for all known encoder names.
    Each entry: (key, [search_strings]) — any match = available.

    v4 STABILITY FIX: the v3 search strings for libsvtav1 and libaom were
    wrong. ffmpeg's `-encoders` output lists them as `libsvtav1` and
    `libaom-av1` (no underscore between svt/av1, hyphen between aom/av1) —
    NOT `libsvt_av1` / `libaom_av1`. This caused _probe_ffmpeg_libs to
    report False for both even when they were installed, which then caused
    _handle_vs_incompat to incorrectly tell the user "ffmpeg also lacks
    libsvtav1" and abort — even though ffmpeg actually had it. The e2e
    test test_probe_detects_ffmpeg_libs caught this.
    """
    try:
        res = subprocess.run(
            [ffmpeg_bin, "-encoders"],
            capture_output=True, text=True, timeout=10,
        )
        output = res.stdout
    except (OSError, subprocess.SubprocessError):
        output = ""

    # v4: search strings match the EXACT names ffmpeg -encoders prints.
    # Verified against ffmpeg 7.x output:
    #   V..... libsvtav1            SVT-AV1(...) encoder (codec av1)
    #   V....D libaom-av1           libaom AV1 (codec av1)
    #   V....D libvpx-vp9           libvpx VP9 (codec vp9)
    # The trailing space in each search string anchors the match to the
    # encoder name boundary, preventing false positives like "libvpx_vp9"
    # matching "libvpx_vp9_decoder" (which doesn't exist, but defensive).
    # We also include the underscore variant as a fallback for older
    # ffmpeg builds that may have used that spelling.
    checks = [
        ("libsvtav1", ["libsvtav1 ", "libsvt_av1", "svt_av1 "]),
        ("libaom",    ["libaom-av1 ", "libaom_av1", "aom_av1 "]),
        ("libvpx",    ["libvpx-vp9 ", "libvpx_vp9", "vpx_vp9 "]),
        ("libx265",   ["libx265 "]),
        ("libopus",   ["libopus "]),
        ("libvorbis", ["libvorbis "]),
        ("flac",      ["flac "]),
        # v4.6.0: NVENC hardware encoders (compiled-in check only — the
        # live-encode gate is _probe_gpu()'s functional dict).
        ("hevc_nvenc", ["hevc_nvenc "]),
        ("h264_nvenc", ["h264_nvenc "]),
        ("av1_nvenc",  ["av1_nvenc "]),
    ]
    libs = {}
    for lib_name, search_strings in checks:
        libs[lib_name] = any(s in output for s in search_strings)
    return libs


def _probe_av1an_version(av1an_bin: str) -> str | None:
    """Extract av1an version string."""
    try:
        # Try --version first, fall back to parsing --help header
        for args in (["--version"], ["--help"]):
            res = subprocess.run(
                [av1an_bin] + args,
                capture_output=True, text=True, timeout=10,
            )
            output = res.stdout or res.stderr
            match = re.search(r"av1an\s+([\d.]+(?:-\w+)?)", output, re.IGNORECASE)
            if match:
                return match.group(1)
            if res.stdout.strip():  # If --version produced output but no version match
                return res.stdout.strip().splitlines()[0][:60]
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _probe_ffmpeg_version(ffmpeg_bin: str) -> str | None:
    """Extract ffmpeg version string."""
    try:
        res = subprocess.run(
            [ffmpeg_bin, "-version"],
            capture_output=True, text=True, timeout=10,
        )
        first_line = res.stdout.splitlines()[0] if res.stdout else ""
        match = re.search(r"ffmpeg version (\S+)", first_line)
        return match.group(1) if match else first_line[:60]
    except (OSError, subprocess.SubprocessError):
        return None


def _probe_runtime_deps(distro: DistroProfile) -> tuple[dict[str, bool], list[str]]:
    """Check runtime dependencies that av1an needs to function.
    Returns (deps_dict, missing_pkg_names).
    
    Checks:
      - VapourSynth + VSScript (av1an loads libvapoursynth-script.so via dlopen
        to get the VSScript API — without this it panics with
        'Failed to get VSScript API')
      - Encoder binaries that av1an invokes directly (svt_av1, x265, vpxenc)
    """
    deps: dict[str, bool] = {}
    missing_pkgs: list[str] = []

    # --- VapourSynth + VSScript (critical: av1an will panic without it) ---
    # av1an is a Rust binary that dlopen's libvapoursynth-script.so and calls
    # vsscript_init() / vsscript_createScript() / etc.  It does NOT use the
    # Python vapoursynth module.  The shared library and the VSScript API
    # library can be packaged separately on some distros (e.g. Debian has
    # libvapoursynth-script-dev).  We must check what av1an actually loads.
    #
    # IMPORTANT: We do NOT call vsscript_init() in our probe.  VSScript's init
    # internally calls Py_Initialize(), which crashes/fails when Python is
    # already running (our probe runs inside a Python subprocess).  Instead,
    # we verify the shared library exists AND can be dlopen'd (CDLL constructor
    # resolves all .so dependencies).  If it loads, it will work for av1an.
    vs_ok = False
    vs_detail = ""
    vs_ver_str = ""
    vs_lib_path = None

    # --- Step 1: Direct filesystem check (most reliable) ---
    # Check well-known install paths. Works even if ldconfig cache is stale.
    _vs_script_search = [
        "/usr/lib/libvapoursynth-script.so",
        "/usr/lib/libvapoursynth_script.so",
        "/usr/lib64/libvapoursynth-script.so",
        "/usr/lib/x86_64-linux-gnu/libvapoursynth-script.so",
        "/usr/local/lib/libvapoursynth-script.so",
    ]
    for p in _vs_script_search:
        if Path(p).is_file():
            vs_lib_path = p
            break

    # --- Step 2: Glob search on known lib dirs ---
    if not vs_lib_path:
        for lib_dir in ("/usr/lib", "/usr/lib64", "/usr/local/lib",
                        "/usr/lib/x86_64-linux-gnu"):
            d = Path(lib_dir)
            if d.is_dir():
                matches = list(d.glob("libvapoursynth-script.so*"))
                # Prefer unversioned .so over .so.0 (dev symlink)
                for m in sorted(matches, key=lambda p: p.name):
                    vs_lib_path = str(m)
                    break
                if vs_lib_path:
                    break

    # --- Step 3: ldconfig -p ---
    if not vs_lib_path:
        try:
            res = subprocess.run(
                ["ldconfig", "-p"], capture_output=True, text=True, timeout=5,
            )
            for line in res.stdout.splitlines():
                if "libvapoursynth-script" in line or "libvapoursynth_script" in line:
                    parts = line.split("=>")
                    if len(parts) >= 2:
                        vs_lib_path = parts[1].strip().split()[0]
                        break
        except (OSError, subprocess.SubprocessError):
            pass

    # --- Step 4: ctypes.util.find_library ---
    if not vs_lib_path:
        try:
            for name in ("vapoursynth-script", "vapoursynth_script"):
                found = ctypes.util.find_library(name)
                if found:
                    vs_lib_path = found
                    break
        except (OSError, subprocess.SubprocessError):
            pass

    # --- Step 5: Distro-specific package file listing ---
    if not vs_lib_path:
        pkg_query = {
            "arch":   ["pacman", "-Ql", "vapoursynth"],
            "debian": ["dpkg", "-L", "vapoursynth"],
            "redhat": ["rpm", "-ql", "vapoursynth"],
            "suse":   ["rpm", "-ql", "vapoursynth"],
        }
        query_cmd = pkg_query.get(distro.family)
        if query_cmd:
            try:
                res = subprocess.run(
                    query_cmd, capture_output=True, text=True, timeout=10,
                )
                for line in res.stdout.splitlines():
                    line = line.strip()
                    # Skip directory entries and grab .so files
                    if "libvapoursynth-script" in line and line.endswith(".so"):
                        vs_lib_path = line
                        break
                    if "libvapoursynth-script" in line and ".so." in line and not vs_lib_path:
                        vs_lib_path = line  # versioned .so as fallback
            except (OSError, subprocess.SubprocessError):
                pass

    # --- Step 6: dlopen smoke test (diagnostic only, NOT a gate) ---
    # We do NOT gate on dlopen success.  The library's constructor may call
    # Py_Initialize() which conflicts with our Python subprocess, causing a
    # silent segfault.  av1an loads this library in its own fresh Rust process
    # where no Python is running — so it works there even if our probe crashes.
    # We only use dlopen to produce an optional warning.
    vs_dlopen_warning = ""
    if vs_lib_path:
        try:
            _escaped = vs_lib_path.replace("'", "\\'")
            probe_code = (
                "import ctypes; "
                f"try: h = ctypes.CDLL('{_escaped}'); print('LOAD_OK') "
                f"except OSError as e: print(f'LOAD_FAIL|{{e}}') "
                f"except Exception as e: print(f'LOAD_OTHER|{{e}}') "
            )
            res = subprocess.run(
                [sys.executable, "-c", probe_code],
                capture_output=True, text=True, timeout=10,
            )
            out = res.stdout.strip()
            if out == "LOAD_OK":
                vs_ok = True
            elif out:
                vs_dlopen_warning = f"dlopen test failed: {out}"
                vs_ok = True  # file exists — let av1an try in its own process
            else:
                # subprocess produced no output — likely segfault in library
                # constructor (Py_Initialize conflict). File still exists.
                vs_dlopen_warning = "dlopen test produced no output (likely segfault in library constructor — not a problem for av1an)"
                vs_ok = True
        except subprocess.TimeoutExpired:
            vs_dlopen_warning = "dlopen test timed out (library may have hanging constructor)"
            vs_ok = True
        except (OSError, subprocess.SubprocessError) as e:
            vs_dlopen_warning = f"dlopen probe error: {e}"
            vs_ok = True

    # Final gate: library file was found on disk
    if vs_lib_path and not vs_ok:
        vs_ok = True  # file found on disk is sufficient

    if vs_ok:
        vs_detail = vs_lib_path or "found"
        # Try to get VapourSynth version from the core lib for diagnostics
        try:
            ver_probe = (
                "import ctypes, ctypes.util; "
                "_lib = ctypes.util.find_library('vapoursynth'); "
                "if not _lib: "
                "  import subprocess as _sp; "
                "  _r = _sp.run(['ldconfig','-p'], capture_output=True, text=True, timeout=5); "
                "  _m = [l.split('=>')[1].strip().split()[0] for l in _r.stdout.splitlines() "
                "       if 'libvapoursynth.so.' in l and 'script' not in l]; "
                "  _lib = _m[0] if _m else None; "
                "if _lib: "
                "  try: "
                "    _h = ctypes.CDLL(_lib); "
                "    _fn = _h.vapoursynth_version; "
                "    _fn.restype = ctypes.c_int; "
                "    print(_fn()) "
                "  except: pass "
            )
            res = subprocess.run(
                [sys.executable, "-c", ver_probe],
                capture_output=True, text=True, timeout=10,
            )
            ver_out = res.stdout.strip()
            if ver_out and ver_out.isdigit() and int(ver_out) > 0:
                vs_ver_str = f"R{ver_out}"
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        if not vs_detail:
            vs_detail = "libvapoursynth-script.so not found (checked filesystem, ldconfig, and package manager)"

    deps["vapoursynth"] = vs_ok
    if not vs_ok:
        # Determine which package(s) to suggest.
        # Most distros bundle VSScript into the main 'vapoursynth' package,
        # but some split it (Debian/Ubuntu: libvapoursynth-script-dev).
        # Use the dedicated vsscript_pkg field if set, else fall back to dep_pkgs.
        if distro.vsscript_pkg:
            missing_pkgs.append(distro.vsscript_pkg)
        elif "vapoursynth" in distro.dep_pkgs:
            missing_pkgs.append(distro.dep_pkgs["vapoursynth"])
        deps["vs_detail"] = False  # extra key for the diagnostic message
    else:
        deps["vs_detail"] = True

    # --- Encoder binaries (av1an invokes these directly, not via ffmpeg) ---
    for enc_key, binary_names in distro.encoder_binaries.items():
        found = False
        for bin_name in binary_names:
            if _find_binary(bin_name, distro) is not None:
                found = True
                break
        deps[enc_key] = found
        if not found:
            # Map encoder key to dep_pkgs key
            dep_key_map = {"svt_av1": "svt-av1", "vpx": "vpx", "x265": "x265"}
            dep_key = dep_key_map.get(enc_key, enc_key)
            if dep_key in distro.dep_pkgs:
                pkg_name = distro.dep_pkgs[dep_key]
                if pkg_name not in missing_pkgs:
                    missing_pkgs.append(pkg_name)

    # --- ffprobe (needed for input file validation) ---
    # Already checked in probe_environment() for the main binary, but let's
    # make sure the dep dict reflects it for consistency.
    # (ffprobe_path is set separately in probe_environment)

    return deps, missing_pkgs, vs_detail, vs_ver_str, vs_dlopen_warning


def probe_environment() -> EnvProbe:
    """
    Distro-aware binary detection + av1an flag compatibility probe +
    ffmpeg library availability check.
    """
    distro = detect_distro()
    result = EnvProbe(distro=distro)
    result.cpu = detect_cpu_topology()
    cpu = result.cpu

    result.warnings.append(f"Detected distro: {distro.name} (family={distro.family}, v{distro.version_id})")
    result.warnings.append(
        f"CPU: {cpu.model_name} — {cpu.physical_cores} physical cores x {cpu.threads_per_core} threads = {cpu.logical_threads} logical"
    )

    # --- Binary detection (distro-aware path search) ---
    for name, attr in [("av1an", "av1an_path"), ("ffmpeg", "ffmpeg_path"), ("ffprobe", "ffprobe_path")]:
        path = _find_binary(name, distro)
        if path is None:
            result.errors.append(f"Missing binary: {name}")
        else:
            setattr(result, attr, path)

    # --- Install hint for missing binaries ---
    if result.install_hint:
        result.warnings.append(f"Install command: {result.install_hint}")

    # --- FFmpeg version + library probe ---
    if result.ffmpeg_path:
        result.ffmpeg_version = _probe_ffmpeg_version(result.ffmpeg_path)
        if result.ffmpeg_version:
            result.warnings.append(f"FFmpeg version: {result.ffmpeg_version}")
        result.ffmpeg_libs = _probe_ffmpeg_libs(result.ffmpeg_path)
        result.ffmpeg_libs = _probe_ffmpeg_libs(result.ffmpeg_path)

        # v4.6.0: GPU/NVENC probe — compiled-in check + live encode smoke
        # test. EncoderWorker gates the GPU path on env.gpu.functional;
        # this warning block surfaces the result (and the fix when the
        # driver is too old for the ffmpeg build's NVENC API).
        result.gpu = _probe_gpu(result.ffmpeg_path)
        gpu = result.gpu
        if gpu.has_gpu:
            result.warnings.append(
                f"GPU: {gpu.name or 'NVIDIA'} — NVENC ready: "
                f"{', '.join(gpu.usable_encoders)} (engine: Auto will use the GPU)"
            )
        elif gpu.encoders and any(gpu.encoders.values()):
            present = [e for e in _NVENC_ENCODER_NAMES if gpu.encoders.get(e)]
            detail = gpu.first_failure_detail
            result.warnings.append(
                f"GPU: NVENC encoder(s) {', '.join(present)} present in ffmpeg "
                f"but NOT usable — {detail or 'smoke test failed'}. "
                f"Auto engine will fall back to CPU."
            )
            if "API version" in detail or "minimum required Nvidia driver" in detail:
                result.warnings.append(
                    "  FIX: update the NVIDIA driver (the ffmpeg build's NVENC "
                    "API is newer than the installed driver supports), or use "
                    "an ffmpeg build matching the installed driver."
                )
        else:
            result.warnings.append(
                "GPU: no hardware encoder in this ffmpeg build — CPU encoding."
            )

        # Warn about missing AUDIO libs (video codecs are handled by av1an's own
        # encoder binaries — ffmpeg's video encoder list is irrelevant)
        audio_lib_warnings = {
            "Opus": "libopus",
            "Vorbis": "libvorbis",
            "FLAC": "flac",
        }
        for codec_label, lib_name in audio_lib_warnings.items():
            if not result.ffmpeg_libs.get(lib_name, False):
                result.warnings.append(f"FFmpeg missing encoder: {lib_name} ({codec_label} audio will not work)")

    # --- Av1an version ---
    if result.av1an_path:
        result.av1an_version = _probe_av1an_version(result.av1an_path)
        if result.av1an_version:
            result.warnings.append(f"av1an version: {result.av1an_version}")

    # --- Av1an flag compatibility probe ---
    if result.av1an_path:
        try:
            help_out = subprocess.run(
                [result.av1an_path, "--help"],
                capture_output=True, text=True, timeout=15,
            ).stdout
            result.av1an_flags = {
                "worker": "--workers" if "--workers" in help_out else "-w",
                "video_params": "--video-params" if "--video-params" in help_out else "-v",
                "audio_params": "--audio-params" if "--audio-params" in help_out else "-a",
            }

            # Detect which encoder names this av1an build actually accepts.
            # Substring matching on --help is unreliable (e.g. "svt" appears in
            # descriptions but the real name may be "svtav1" or "svt_av1").
            # Instead, pass a bogus encoder name and parse the clap error which
            # lists all valid values.
            svt_name = _detect_av1an_svt_encoder(result.av1an_path)
            if svt_name:
                result.av1an_flags["svt_name"] = svt_name
                result.warnings.append(f"av1an SVT-AV1 encoder name: '{svt_name}'")
            else:
                # Absolute fallback — should rarely be needed
                result.av1an_flags["svt_name"] = "svt_av1"
                result.warnings.append("av1an SVT-AV1 encoder name: 'svt_av1' (fallback, not auto-detected)")

            # Check for chunk-method availability (differs by av1an version/distro)
            if "--chunk-method" in help_out:
                result.av1an_flags["has_chunk_method"] = True

            # Check for --temp flag (lets us relocate av1an work dir out of user folders)
            if "--temp" in help_out:
                result.av1an_flags["has_temp"] = True
                result.av1an_flags["temp_flag"] = "--temp"
            elif "-T" in help_out:
                result.av1an_flags["has_temp"] = True
                result.av1an_flags["temp_flag"] = "-T"

            # Check for -s/segments flag (newer av1an)
            if "-s" in help_out or "--scenes" in help_out:
                result.av1an_flags["has_scenes"] = True

            # Detect concat method: prefer mkvmerge, fall back to ffmpeg
            if shutil.which("mkvmerge"):
                result.av1an_flags["concat_method"] = "mkvmerge"
            else:
                result.av1an_flags["concat_method"] = "ffmpeg"

            # v4.0.0: Probe VapourSynth source plugins. When NONE of the
            # source plugins (lsmash, ffms2, bestsource, dgdecnv) are
            # installed, av1an falls back to the Hybrid chunk method —
            # which fails on phone-recorded MP4s with sparse keyframes
            # (the "works up until near the end, never saves chunks into
            # a full file" bug). Pre-setting chunk_method_override="select"
            # avoids the wasted first-attempt + retry on every file.
            #
            # The select method uses VapourSynth's select() filter to
            # extract frames one-by-one — slower than ffms2/bestsource
            # but reliable for any file VapourSynth can open.
            vs_plugins = _probe_vs_source_plugins()
            result.av1an_flags["vs_plugins"] = vs_plugins
            if vs_plugins:
                result.warnings.append(
                    f"VapourSynth source plugins: {', '.join(vs_plugins)} "
                    f"— av1an will auto-select a fast chunk method"
                )
            else:
                result.warnings.append(
                    "VapourSynth source plugins: NONE found — "
                    "forcing --chunk-method select (reliable but slower). "
                    "Install vapoursynth-{lsmash,ffms2,bestsource} for faster "
                    "chunk-parallel encoding."
                )
                result.av1an_flags["chunk_method_override"] = "select"
                result.av1an_flags["chunk_method_override"] = "select"

            # v4.6.0: ffmpeg ≥ 7 removed the -vsync option that av1an's
            # segment/hybrid chunk extraction passes to ffmpeg. On those
            # systems every segment-based chunk dies immediately with
            # "Unrecognized option 'vsync'." — the y4m pipe breaks and
            # each chunk fails 3x. The select override above already
            # avoids those methods when no plugins are installed; this
            # warning tells plugin-less users on new ffmpeg WHY av1an is
            # stuck on slow select.
            if not vs_plugins and result.ffmpeg_version:
                try:
                    ffmpeg_major = int(
                        re.match(r"[nN]?(\d+)", result.ffmpeg_version).group(1)
                    )
                except (AttributeError, ValueError):
                    ffmpeg_major = 0
                if ffmpeg_major >= 7:
                    result.warnings.append(
                        "av1an note: ffmpeg ≥ 7 removed the -vsync option av1an's "
                        "segment/hybrid chunk methods use — those methods fail "
                        "with \"Unrecognized option 'vsync'\". Chunking stays on "
                        "'select'. Install a VapourSynth source plugin "
                        "(bestsource/ffms2/lsmash) to escape slow select, or use "
                        "the default ffmpeg-only path."
                    )

        except (OSError, subprocess.SubprocessError) as e:
            result.errors.append(f"av1an probe failed: {e}")

    # --- Distro-specific notes ---
    if distro.notes:
        result.warnings.append(f"Distro note: {distro.notes}")

    # --- Runtime dependency probe (vapoursynth, encoder binaries) ---
    if result.av1an_path:
        deps, missing_pkgs, vs_detail, vs_ver, vs_dlopen_warn = _probe_runtime_deps(distro)
        result.runtime_deps = deps
        result.missing_dep_pkgs = missing_pkgs
        if deps.get("vapoursynth"):
            result.vs_version = vs_ver
            result.vs_script_lib = vs_detail

        # Log VapourSynth/VSScript with extra detail
        vs_status = "OK" if deps.get("vapoursynth") else "MISSING"
        result.warnings.append(f"Dependency: vapoursynth (VSScript API) = {vs_status}")
        if deps.get("vapoursynth"):
            # vs_detail is the library path on success
            result.warnings.append(f"  VSScript lib: {vs_detail}")
            if vs_dlopen_warn:
                result.warnings.append(f"  dlopen note: {vs_dlopen_warn}")
        else:
            # vs_detail is the failure reason
            result.warnings.append(f"  Reason: {vs_detail}")

        # Log encoder binary deps (skip vs_detail key)
        for dep_name, present in deps.items():
            if dep_name in ("vapoursynth", "vs_detail"):
                continue
            status = "OK" if present else "MISSING"
            result.warnings.append(f"Dependency: {dep_name} = {status}")

        if missing_pkgs:
            hint = result.dep_install_hint
            result.errors.append(
                f"Missing runtime dependencies: {', '.join(missing_pkgs)}"
            )
            if hint:
                result.errors.append(f"  FIX: {hint}")

    return result


# ──────────────────────────────────────────────
#  FFPREPBE VALIDATION
# ──────────────────────────────────────────────

def ffprobe_validate(filepath: Path, ffprobe_bin: str) -> dict[str, object] | None:
    """Returns stream info dict or None if invalid/unreadable."""
    try:
        res = subprocess.run(
            [ffprobe_bin, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(filepath)],
            capture_output=True, text=True, timeout=30,
        )
        if res.returncode != 0:
            return None
        return json.loads(res.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        # ValueError covers json.JSONDecodeError
        return None


def ffprobe_duration(filepath: Path, ffprobe_bin: str) -> float | None:
    """Return media duration in seconds via ffprobe, or None on failure.

    Used by the EncoderWorker post-encode integrity check to compare source
    and output durations. Modeled after :func:`ffprobe_validate` — every
    failure path returns ``None`` so the caller can treat unverifiable
    durations as "skip the check" rather than crashing the worker thread.
    """
    try:
        res = subprocess.run(
            [ffprobe_bin, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_entries", "format=duration",
             str(filepath)],
            capture_output=True, text=True, timeout=10,
        )
        if res.returncode != 0 or not res.stdout:
            return None
        data = json.loads(res.stdout)
        dur_str = (data.get("format") or {}).get("duration")
        if dur_str is None:
            return None
        return float(dur_str)
    except (OSError, subprocess.SubprocessError, ValueError):
        # ValueError covers json.JSONDecodeError and float() parse failures
        return None


def _verify_output_resolution(output_path: Path, ffprobe_bin: str, target_w: int, target_h: int) -> bool:
    """Verify that an encoded file actually has the requested output resolution.

    Returns True if the output matches (or is within 2px due to force_divisible_by=2),
    False otherwise.
    """
    try:
        res = subprocess.run(
            [ffprobe_bin, "-v", "quiet", "-print_format", "json",
             "-show_streams", "-select_streams", "v:0", str(output_path)],
            capture_output=True, text=True, timeout=15,
        )
        if res.returncode != 0:
            return True  # can't verify, don't block
        data = json.loads(res.stdout)
        streams = data.get("streams", [])
        if not streams:
            return True
        ow = int(streams[0].get("width", 0) or 0)
        oh = int(streams[0].get("height", 0) or 0)
        # Allow 2px tolerance (force_divisible_by=2 rounding)
        if abs(ow - target_w) <= 2 and abs(oh - target_h) <= 2:
            return True
        return False
    except (OSError, subprocess.SubprocessError, ValueError):
        # ValueError covers json.JSONDecodeError and int() parse failures
        return True  # can't verify, don't block


def _identify_file_type(file_path: Path) -> str:
    """Run `file` on the given path and return the type string.

    v5-03: Used by _validate_file to tell the user WHAT a file actually is
    when ffprobe can't read it. This immediately reveals:
      - "HTML document" → failed yt-dlp download (YouTube error page saved as .mp4)
      - "ASCII text" → same as above (different yt-dlp version)
      - "data" → truncated, encrypted, or partial download
      - "ISO Media, MP4 Base Media v1" → valid MP4 that ffprobe just can't parse (rare)

    Returns the first line of `file` output (minus the filename prefix),
    or an empty string if `file` is not available or fails.
    """
    file_bin = shutil.which("file")
    if not file_bin:
        return ""
    try:
        res = subprocess.run(
            [file_bin, "-b", str(file_path)],
            capture_output=True, text=True, timeout=5,
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def _detect_av1an_svt_encoder(av1an_bin: str) -> str | None:
    """Determine the exact encoder name av1an accepts for SVT-AV1.

    Strategy (in order):
      1. Run ``av1an --encoder __PROBE__`` and parse clap's error for
         ``[possible values: ...]``.
      2. Parse ``--help`` for ``[default: <name>]`` next to ``--encoder``.
      3. Regex fallback on the error output.
    """
    try:
        # --- Method 1: clap error with possible values ---
        res = subprocess.run(
            [av1an_bin, "--encoder", "__PROBE_TEST__"],
            capture_output=True, text=True, timeout=10,
        )
        stderr = res.stderr or ""
        stdout = res.stdout or ""
        combined = stderr + stdout

        m = re.search(r"\[possible values:\s*([^\]]+)\]", combined)
        if m:
            values = [v.strip().rstrip(',') for v in m.group(1).split()]
            for v in values:
                if "svt" in v.lower():
                    return v

        # --- Method 2: parse --help for encoder default value ---
        help_res = subprocess.run(
            [av1an_bin, "--help"],
            capture_output=True, text=True, timeout=10,
        )
        help_text = (help_res.stdout or "") + (help_res.stderr or "")
        # Look for pattern: --encoder <ENCODER> ... [default: svt-av1]
        m2 = re.search(
            r"--encoder\s+<ENCODER>.*?\[default:\s*(\S+?)\]",
            help_text, re.DOTALL,
        )
        if m2:
            return m2.group(1)

        # --- Method 3: regex fallback on the error output ---
        for line in combined.splitlines():
            for token in re.findall(r"\bsvt[a-z_-]*av1[a-z_-]*\b", line, re.IGNORECASE):
                return token
            for token in re.findall(r"\bsvtav1\b", line, re.IGNORECASE):
                return token

        return None
    except (OSError, subprocess.SubprocessError):
        return None


def _av1an_env() -> dict[str, str]:
    """Build an env dict for subprocess that includes ~/.local/lib in LD_LIBRARY_PATH.

    When VapourSynth is built from git and installed to ~/.local/, the linker
    won't find libvapoursynth-script.so unless LD_LIBRARY_PATH points there.
    This function ensures every av1an invocation inherits that path.

    v4.7.1: the git VS stack is self-contained in the python user
    site-packages (module + libs + BestSource plugin), so the runtime env
    also gets that dir on LD_LIBRARY_PATH and the user site on PYTHONPATH —
    otherwise av1an loads the system VS and never sees the fresh stack.
    """
    env = os.environ.copy()
    local_lib = str(Path.home() / ".local" / "lib")
    existing = env.get("LD_LIBRARY_PATH", "")
    if local_lib not in existing:
        env["LD_LIBRARY_PATH"] = f"{local_lib}:{existing}".rstrip(":")
    try:
        user_site = Path(site.getusersitepackages())
        vs_dir = user_site / "vapoursynth"
        if vs_dir.is_dir() and (vs_dir / "libvsscript.so").exists():
            existing = env.get("LD_LIBRARY_PATH", "")
            if str(vs_dir) not in existing:
                env["LD_LIBRARY_PATH"] = f"{vs_dir}:{existing}".rstrip(":")
            py_path = env.get("PYTHONPATH", "")
            if str(user_site) not in py_path:
                env["PYTHONPATH"] = f"{user_site}:{py_path}".rstrip(":")
    except (AttributeError, OSError):
        pass
    return env


# v4.0.0: VapourSynth source plugin probe. Returns a list of available
# plugin names (e.g. ["lsmash", "ffms2", "bestsource"]). When the list
# is empty, av1an falls back to the Hybrid chunk method — which fails
# on phone-recorded MP4s with sparse keyframes. The caller uses this
# to decide whether to pre-set chunk_method_override="select".
_VS_PLUGIN_PROBE_PATHS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # (plugin_name, candidate .so filenames)
    # lsmash: imported as `havsfmt` / `lsmas` in VS; .so is libvslsmashsource.so
    ("lsmash",     ("libvslsmashsource.so",)),
    # ffms2: imported as `ffms2` in VS; .so is libffms2.so (sometimes libvffms2.so)
    ("ffms2",      ("libffms2.so", "libvffms2.so")),
    # bestsource: imported as `bestsource` / `bs` in VS
    ("bestsource", ("libbestsource.so", "libvsbestsource.so")),
    # dgdecnv: NVIDIA hardware-accelerated decoder
    ("dgdecnv",    ("libdgdecnv.so",)),
    # vszip: high-performance resize/format plugins
    ("vszip",      ("libvszip.so",)),
)


def _probe_vs_source_plugins() -> list[str]:
    """Probe for VapourSynth source plugins in standard locations.

    Searches (in order):
      1. ``$XDG_DATA_HOME/vapoursynth/`` (or ``~/.local/share/vapoursynth/``)
      2. ``~/.local/lib/vapoursynth/`` (user-installed plugins from source)
      3. ``/usr/lib/vapoursynth/`` (distro-installed plugins)
      4. ``/usr/local/lib/vapoursynth/`` (manually installed)
      5. ``/usr/lib/x86_64-linux-gnu/vapoursynth/`` (Debian multiarch)

    Returns a sorted list of available plugin names. Empty list = no
    source plugins found, which means av1an will fall back to Hybrid
    chunk method and likely fail on phone-recorded MP4s.

    Pure-stdlib (no vapoursynth Python bindings required). Best-effort:
    if a plugin is installed but not in these paths, this probe will
    miss it — but the av1an runtime will still detect it, and the
    v4.0.0 retry in _encode_one will still switch to select on first
    failure.
    """
    search_dirs: list[Path] = []
    xdg_data = os.environ.get("XDG_DATA_HOME", "")
    if xdg_data:
        search_dirs.append(Path(xdg_data) / "vapoursynth")
    else:
        search_dirs.append(Path.home() / ".local" / "share" / "vapoursynth")
    search_dirs.append(Path.home() / ".local" / "lib" / "vapoursynth")
    search_dirs.append(Path("/usr/lib/vapoursynth"))
    search_dirs.append(Path("/usr/local/lib/vapoursynth"))
    search_dirs.append(Path("/usr/lib/x86_64-linux-gnu/vapoursynth"))
    # v4.7.1: the git-built VapourSynth stack installs its plugins into
    # the python site-packages tree (module + libs + plugins/ are one
    # self-contained unit). Probe those dirs too.
    try:
        search_dirs.append(Path(site.getusersitepackages()) / "vapoursynth" / "plugins")
    except (AttributeError, OSError):
        pass
    try:
        for d in site.getsitepackages():
            search_dirs.append(Path(d) / "vapoursynth" / "plugins")
    except (AttributeError, OSError):
        pass

    found: set[str] = set()
    for d in search_dirs:
        if not d.is_dir():
            continue
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_file():
                continue
            name_lower = entry.name.lower()
            for plugin_name, so_names in _VS_PLUGIN_PROBE_PATHS:
                for so_name in so_names:
                    if so_name in name_lower:
                        found.add(plugin_name)
                        break

    return sorted(found)


def _av1an_vsscript_smoke_test(
    av1an_bin: str,
    ffmpeg_bin: str,
    av1an_flags: dict,
    svt_name: str = "svt_av1",
    timeout: int = 30,
) -> tuple[bool, str]:
    """Pre-flight test: create a tiny video and try to run av1an on it.

    This catches 'Failed to get VSScript API' panics BEFORE the real queue
    starts.  File-existence checks for libvapoursynth-script.so pass even
    when the ABI is incompatible (av1an's Rust vapoursynth crate built
    against a different VS version).  Only actually invoking av1an reveals
    the mismatch.

    Returns (ok, detail_message).
      ok=True   -> av1an initialized VSScript successfully.
      ok=False  -> av1an panicked or failed; detail_message explains why.
    """

    with tempfile.TemporaryDirectory(prefix="av1an_smoke_") as tmpdir:
        test_in = Path(tmpdir) / "test_smoke.mkv"
        test_out = Path(tmpdir) / "test_smoke_out.mkv"

        # Create a 1-second 64x64 black video (video-only is enough to
        # trigger VSScript init in av1an — no audio needed).
        gen_cmd = [
            ffmpeg_bin,
            "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1:r=24",
            "-t", "1", "-pix_fmt", "yuv420p", "-an", "-y", str(test_in),
        ]
        try:
            res = subprocess.run(gen_cmd, capture_output=True, text=True, timeout=15)
            if res.returncode != 0:
                return False, f"ffmpeg test-video failed (rc={res.returncode}): {(res.stderr or '')[-200:]}"
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"Could not generate smoke test video: {e}"

        if not test_in.exists():
            return False, "Smoke test video was not created by ffmpeg"

        # Build minimal av1an command
        worker_flag = av1an_flags.get("worker", "--workers")
        vparams_flag = av1an_flags.get("video_params", "--video-params")
        aparams_flag = av1an_flags.get("audio_params", "--audio-params")

        cmd = [
            av1an_bin,
            "-i", str(test_in),
            worker_flag, "1",
            "--encoder", svt_name,
            vparams_flag, "--preset 8 --crf 40 --keyint 240",
            "-o", str(test_out),
        ]

        # Use chunk-method select if available (triggers VSScript init)
        if av1an_flags.get("has_chunk_method"):
            cmd.extend(["--chunk-method", "select"])

        # SEI CERT ERR01-C: catch only the specific exception types we
        # expect from subprocess.run; never swallow unrelated failures.
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                env=_av1an_env())
        except subprocess.TimeoutExpired:
            # Timeout is a real failure — av1an is hanging. Do NOT mask it.
            return False, f"SMOKE_TIMEOUT: av1an smoke test exceeded {timeout}s — likely hung in VSScript init or encoder spawn"
        except FileNotFoundError as e:
            return False, f"SMOKE_BIN_MISSING: {e}"
        except OSError as e:
            return False, f"SMOKE_OS_ERROR: {e}"

        stderr = res.stderr or ""
        stdout = res.stdout or ""

        # Success requires BOTH rc==0 AND the output file actually exists.
        # The previous code returned True on any non-VSScript failure, which
        # masked real bugs (missing encoder binary, concat failure, etc.)
        # and led to "chunks but never saves a file" symptoms in production.
        if res.returncode == 0 and test_out.exists():
            test_out.unlink(missing_ok=True)
            return True, "av1an VSScript init OK"

        # Classify the known failure modes by inspecting stderr.
        if "Failed to get VSScript API" in stderr:
            return False, "VSScript_API_INCOMPAT"

        if "invalid value" in stderr and "--encoder" in stderr:
            return False, f"INVALID_ENCODER: {stderr[-200:]}"

        if "No usable encoder found" in stderr:
            return False, f"ENCODER_BIN_MISSING: {stderr[-300:]}"

        # Unknown failure — return False so the caller can offer ffmpeg
        # fallback or rebuild. Include the FULL stderr (not just the tail)
        # so the user can see the actual error and the diagnostic patterns
        # below can match on it.
        combined = (stderr + "\n--- stdout ---\n" + stdout)[-1500:]
        return False, f"SMOKE_FAIL(rc={res.returncode}): {combined}"


# ──────────────────────────────────────────────
#  TEMP DIRECTORY MANAGEMENT
# ──────────────────────────────────────────────

_APP_CACHE_DIR: Path | None = None

def _get_app_temp_dir() -> Path:
    """Return the shared temp directory for all intermediate files.

    Priority:
      1. ``~/.cache/OpenTranscode/tmp/``  (XDG-compliant, persistent across reboots)
      2. ``/tmp/OpenTranscode/``           (fallback if home cache is unwritable)

    The directory is created on first call.  All temp intermediates
    (pre-scaled MKVs, av1an work dirs) go here so the user's video
    folders stay clean.

    v3 (OTC-013, SEI CERT FIO09-C): the directory is created with
    ``mode=0o700`` so that other users on the system cannot create
    symlinks inside it (which the cleanup sweep would then follow and
    delete arbitrary files). The mode is verified after creation in
    case the directory already existed with looser permissions.
    """
    global _APP_CACHE_DIR
    if _APP_CACHE_DIR is not None:
        return _APP_CACHE_DIR

    # Try XDG cache dir first
    xdg_cache = os.environ.get("XDG_CACHE_HOME", "")
    if xdg_cache:
        candidate = Path(xdg_cache) / "OpenTranscode" / "tmp"
    else:
        candidate = Path.home() / ".cache" / "OpenTranscode" / "tmp"

    if _mkdir_private(candidate):
        _APP_CACHE_DIR = candidate
        return _APP_CACHE_DIR

    # Fallback: /tmp/OpenTranscode
    fallback = Path("/tmp/OpenTranscode")
    if _mkdir_private(fallback):
        _APP_CACHE_DIR = fallback
        return _APP_CACHE_DIR

    # Last resort: system temp
    _APP_CACHE_DIR = Path(tempfile.gettempdir()) / "OpenTranscode"
    _mkdir_private(_APP_CACHE_DIR)
    return _APP_CACHE_DIR


def _mkdir_private(path: Path) -> bool:
    """Create *path* (and parents) with mode 0o700.

    Returns True on success, False on OSError/PermissionError.

    SEI CERT FIO09-C: if the directory already existed with looser
    permissions (e.g. created by a previous version of this app, or by
    another user before us), we attempt to tighten the mode with
    os.chmod(). The chmod may fail silently if we don't own the dir —
    that's an accepted risk, logged but not fatal.
    """
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        # mkdir(mode=) is masked by umask; explicitly chmod to be sure
        os.chmod(path, 0o700)
        return True
    except (OSError, PermissionError):
        return False


def _worker_temp_dir(worker_pid: int, lane: str = "") -> Path:
    """Return a per-worker temp subdir named by PID.

    v3: each EncoderWorker gets its own subdir under the shared app temp
    dir, so the final cleanup sweep can safely nuke only this worker's
    intermediates without affecting a concurrent worker. The subdir is
    also created with mode=0o700 (FIO09-C).

    v4.7.0: *lane* suffixes the dir ("gpu"/"cpu") for the hybrid
    scheduler's concurrent lanes — both run in the SAME process, so the
    PID alone no longer separates them, and a lane finishing early must
    not sweep the other lane's intermediates out from under it.
    """
    base = _get_app_temp_dir()
    name = f"worker-{worker_pid}" + (f"-{lane}" if lane else "")
    sub = base / name
    _mkdir_private(sub)
    return sub





def _temp_path_for(file_path: Path, suffix: str = ".scaled_tmp.mkv",
                   worker_dir: Path | None = None) -> Path:
    """Build a unique temp path for *file_path* inside the app temp dir.

    Uses a short hash of the original absolute path to avoid collisions
    when files in different subdirs share the same stem.

    v3: if *worker_dir* is provided (per-worker subdir), the temp file
    lands there instead of the shared parent. This isolates concurrent
    workers' intermediates from each other.
    """
    tmp_dir = worker_dir if worker_dir is not None else _get_app_temp_dir()
    # Hash the absolute source path for uniqueness
    path_hash = hashlib.sha256(str(file_path.resolve()).encode()).hexdigest()[:12]
    return tmp_dir / f"{file_path.stem}.{path_hash}{suffix}"


# ──────────────────────────────────────────────
#  KEEP-AWAKE (v6-06: anti-sleep / anti-hibernate)
# ──────────────────────────────────────────────

class KeepAwake:
    """Keep the system awake during a transcode.

    v6-06: Uses systemd-inhibit (preferred) to block sleep/idle at the
    systemd level, plus optional xdotool mouse nudging every 60s as a
    belt-and-suspenders fallback. The user sees a bright-red banner in
    the UI while active. Mouse movement is minimal (1px jitter, not
    constant) so the user can still click STOP or close the window.

    Usage::
        ka = KeepAwake(log_fn=worker.log_msg.emit)
        ka.start()
        try:
            while encoding:
                ka.update_eta(remaining_seconds)
                time.sleep(5)
        finally:
            ka.stop()
    """

    def __init__(
        self,
        log_fn=None,
        enable_mouse_nudge: bool = False,
        nudge_interval: int = 60,
    ):
        self._log_fn = log_fn or (lambda msg: None)
        self._enable_mouse_nudge = enable_mouse_nudge and bool(shutil.which("xdotool"))
        self._nudge_interval = nudge_interval
        self._inhibit_proc: subprocess.Popen | None = None
        self._nudge_count = 0
        self._last_nudge = 0.0
        self._start_time = 0.0
        self._eta_seconds: float | None = None
        self._active = False

    def start(self) -> None:
        """Acquire systemd-inhibit handle. Safe to call multiple times."""
        if self._active:
            return
        self._active = True
        self._start_time = time.monotonic()
        self._acquire_inhibit()
        if self._enable_mouse_nudge:
            self._log_fn("KEEP-AWAKE: mouse nudging enabled (xdotool, every "
                         f"{self._nudge_interval}s)")
        else:
            self._log_fn("KEEP-AWAKE: mouse nudging disabled (xdotool not found "
                         "or not requested)")

    def stop(self) -> None:
        """Release the inhibit handle and stop nudging."""
        if not self._active:
            return
        self._active = False
        self._release_inhibit()
        if self._nudge_count > 0:
            self._log_fn(f"KEEP-AWAKE: stopped (mouse nudged {self._nudge_count} times)")

    def update_eta(self, remaining_seconds: float | None) -> None:
        """Update the ETA shown in the banner. None = unknown."""
        self._eta_seconds = remaining_seconds

    def tick(self) -> str | None:
        """Called periodically (e.g. every 5s) from the UI thread.

        Performs mouse nudge if interval has elapsed.
        Returns the current banner text, or None if keep-awake is not active.
        """
        if not self._active:
            return None
        now = time.monotonic()
        if self._enable_mouse_nudge and (now - self._last_nudge) >= self._nudge_interval:
            self._nudge_mouse()
            self._last_nudge = now
        return self.banner_text()

    def banner_text(self) -> str:
        """Return the banner text for the UI (styled bright-red in QSS)."""
        eta_str = self._format_eta(self._eta_seconds)
        elapsed = time.monotonic() - self._start_time
        elapsed_str = self._format_eta(elapsed)
        nudge_str = f" | mouse: {self._nudge_count}" if self._nudge_count > 0 else ""
        return (
            f"KEEP-AWAKE ACTIVE — system will not sleep | "
            f"elapsed: {elapsed_str} | ETA: {eta_str}{nudge_str}"
        )

    def _format_eta(self, seconds: float | None) -> str:
        if seconds is None:
            return "unknown"
        if seconds < 0:
            return "almost done"
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        if hours > 0:
            return f"~{hours}h{mins:02d}m"
        if mins > 0:
            return f"~{mins}m{secs:02d}s"
        return f"~{secs}s"

    def _acquire_inhibit(self) -> None:
        """Fork a systemd-inhibit subprocess that holds the sleep/idle
        inhibit handle for the duration of the transcode."""
        inhibit_bin = shutil.which("systemd-inhibit")
        if not inhibit_bin:
            self._log_fn("KEEP-AWAKE: systemd-inhibit not found — "
                         "system may sleep during transcode")
            return
        try:
            self._inhibit_proc = subprocess.Popen(
                [
                    inhibit_bin,
                    "--what=sleep:idle",
                    "--who=OpenTranscode",
                    "--why=Batch video transcode in progress",
                    "--mode=block",
                    "sleep", "infinity",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._log_fn("KEEP-AWAKE: systemd-inhibit active (sleep/idle blocked)")
        except (OSError, subprocess.SubprocessError) as e:
            self._log_fn(f"KEEP-AWAKE: failed to acquire systemd-inhibit: {e}")
            self._inhibit_proc = None

    def _release_inhibit(self) -> None:
        """Kill the systemd-inhibit subprocess to release the handle."""
        if self._inhibit_proc is None:
            return
        try:
            self._inhibit_proc.terminate()
            self._inhibit_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._inhibit_proc.kill()
            self._inhibit_proc.wait(timeout=1)
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            self._inhibit_proc = None
            self._log_fn("KEEP-AWAKE: systemd-inhibit released")

    def _nudge_mouse(self) -> None:
        """Move the mouse 1 pixel to prevent screen-blank.

        Alternates +1px right / -1px left so the cursor ends up where it
        started after every pair of nudges.
        """
        xdotool = shutil.which("xdotool")
        if not xdotool:
            return
        delta = 1 if (self._nudge_count % 2 == 0) else -1
        try:
            subprocess.run(
                [xdotool, "mousemove_relative", "--", str(delta), "0"],
                capture_output=True, timeout=3,
            )
            self._nudge_count += 1
        except (OSError, subprocess.SubprocessError):
            pass  # best-effort — don't crash the transcode over a nudge

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def has_inhibit(self) -> bool:
        return self._inhibit_proc is not None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()


# ──────────────────────────────────────────────
#  INTELLIGENT WORKER-COUNT MATH (v4.1.0)
# ──────────────────────────────────────────────

def _compute_intelligent_worker_count_for(
    env: EnvProbe,
    max_workers: int | None = None,
    threads_per_worker_override: int | None = None,
) -> tuple[int, int]:
    """Compute ``(worker_count, threads_per_worker)`` to prevent thread
    oversubscription on high-core-count machines.

    PROBLEM (v4.0.0 and earlier)
    ----------------------------
    ``run()`` set ``worker_count = max(1, physical_cores - 1)`` and
    passed no per-chunk thread cap to the encoder. SVT-AV1's default
    ``--threads 0`` means "use all logical cores," so each chunk-parallel
    worker spawned an SvtAv1EncApp process that grabbed every logical
    thread. On a 28-thread Xeon (14 physical cores), 13 workers × 28
    threads = ~364 active threads on 28 logical CPUs — the kernel
    scheduler drowns, I/O wait escalates, and the box hard-locks even
    though no single process is at fault. The 1-second STOP-button
    poll in ``_run_with_stop_check`` can't get scheduled, so even
    clicking STOP doesn't recover it.

    v4.0.0 made it WORSE for the phone-video workload because the
    ``--chunk-method select`` auto-override keeps the pipeline tighter
    (no Hybrid warm-up between chunks), so more SVT-AV1 instances hit
    full tilt at the same instant.

    SOLUTION
    --------
    Budget the total thread count to ``logical_threads - 1`` (one
    logical thread reserved for OS/UI), then split that budget across
    chunk-parallel workers. Each encoder instance gets
    ``--threads N`` so it can't grab more than its share.

    Algorithm
    ---------
    1. ``budget = max(1, logical_threads - 1)`` — leave 1 logical
       thread for OS / UI / av1an orchestrator.
    2. ``ideal_tpw = 4`` — empirical sweet spot for SVT-AV1, x265,
       and vpxenc. Beyond ~6 threads per encoder instance you hit
       memory-bandwidth contention and diminishing returns.
    3. ``target_workers = max(1, budget // ideal_tpw)``.
    4. Cap ``target_workers`` at ``max(1, physical_cores - 1)`` so
       chunk-parallel never exceeds the physical core count.
    5. ``threads_per_worker = max(1, budget // target_workers)``.
    6. Apply user overrides (``max_workers`` /
       ``threads_per_worker_override``) if provided.

    Examples
    --------
      4-core / 8-thread laptop:
        budget=7, target_workers=7//4=1, tpw=7//1=7  → 1×7 = 7
      8-core / 16-thread desktop:
        budget=15, target_workers=15//4=3, tpw=15//3=5 → 3×5 = 15
      14-core / 28-thread Xeon (the user's box):
        budget=27, target_workers=27//4=6, tpw=27//6=4 → 6×4 = 24
        (leaves 4 logical threads for OS/UI breathing room)
      32-core / 64-thread EPYC:
        budget=63, target_workers=63//4=15, tpw=63//15=4 → 15×4=60
      1-core / 2-thread VM:
        budget=1, target_workers=1, tpw=1 → 1×1 = 1

    Returns ``(worker_count, threads_per_worker)``. Both are ≥1.
    """
    physical = max(1, env.cpu.physical_cores)
    logical = max(1, env.cpu.logical_threads)

    # User override short-circuit (highest priority).
    if max_workers is not None and threads_per_worker_override is not None:
        wc = max(1, int(max_workers))
        tpw = max(1, int(threads_per_worker_override))
        return wc, tpw

    # Budget: leave 1 logical thread for OS / UI / av1an orchestrator.
    budget = max(1, logical - 1)

    # Ideal threads per encoder instance — empirical sweet spot.
    # v4.1.0 used 4; v4.1.1 bumped to 6 because SVT-AV1 with only 4
    # threads was too slow per-chunk, making the total throughput
    # feel "borked" even though the thread budget was correct.
    # With 6 threads per worker, SVT-AV1 has enough parallelism for
    # motion estimation while staying under the logical-thread budget.
    IDEAL_THREADS_PER_WORKER = 6

    # Target worker count from budget / ideal_tpw.
    target_workers = max(1, budget // IDEAL_THREADS_PER_WORKER)

    # Cap at physical_cores - 1 so chunk-parallel doesn't exceed
    # physical core count (avoids L3 cache thrash on chiplet CPUs).
    max_by_phys = max(1, physical - 1) if physical > 1 else 1
    target_workers = min(target_workers, max_by_phys)

    # Apply --max-workers override if provided (still cap by physical).
    if max_workers is not None:
        target_workers = min(max(1, int(max_workers)), max_by_phys)

    # Compute threads per worker.
    if threads_per_worker_override is not None:
        tpw = max(1, int(threads_per_worker_override))
    else:
        tpw = max(1, budget // target_workers)

    return target_workers, tpw


# ──────────────────────────────────────────────
#  ENCODER WORKER (QThread, from PySide6 ver, extended)
# ──────────────────────────────────────────────

def scan_input_files(in_dir: Path, extensions: set[str]) -> list[Path]:
    """Collect the transcodable files under *in_dir* (v4.7.0).

    Used by ``EncoderWorker.run()`` for the default whole-directory scan
    AND by the hybrid scheduler's pre-scan, which must partition the
    queue BEFORE the per-lane workers are constructed. Excludes leftover
    pre-scale intermediates from previous failed runs.
    """
    return sorted(
        f for f in in_dir.rglob("*")
        if f.is_file()
        and f.suffix.lower() in extensions
        and not f.name.endswith(".scaled_tmp.mkv")
    )




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


def scan_input_files(in_dir: Path, extensions: set[str]) -> list[Path]:
    """Collect the transcodable files under *in_dir* (v4.7.0).

    Used by ``EncoderWorker.run()`` for the default whole-directory scan
    AND by the hybrid scheduler's pre-scan, which must partition the
    queue BEFORE the per-lane workers are constructed. Excludes leftover
    pre-scale intermediates from previous failed runs.
    """
    return sorted(
        f for f in in_dir.rglob("*")
        if f.is_file()
        and f.suffix.lower() in extensions
        and not f.name.endswith(".scaled_tmp.mkv")
    )


def selected_gpu_profile(env):
    """v4.8.0: the active GpuProfile — the UI's dropdown selection
    (env.av1an_flags["gpu_profile"]) when set, else the auto-matched
    profile from the probe (env.gpu.profile_key). None when neither."""
    flags = getattr(env, "av1an_flags", None) or {}
    key = flags.get("gpu_profile")
    if key and key != "auto":
        profile = gpu_profile_by_key(key)
        if profile is not None:
            return profile
    gpu_info = getattr(env, "gpu", None)
    if gpu_info is not None and getattr(gpu_info, "profile_key", ""):
        return gpu_profile_by_key(gpu_info.profile_key)
    return None


def resolve_gpu_encoder(engine: str, video_codec, env):
    """Decide whether this encode runs on the GPU.

    Returns ``(encoder_name, api)`` when the GPU path should be used —
    e.g. ``("hevc_nvenc", "nvenc")`` or ``("hevc_vaapi", "vaapi")`` — or
    ``(None, None)`` for the CPU path.

    Rules:
      - ``engine == "cpu"``              → always CPU (user forced CPU).
      - selected/matched GPU profile has
        no encoder for the codec family  → CPU (e.g. AV1 on Pascal,
                                          VP9 without VAAPI).
      - ``engine`` auto/gpu AND the
        functional probe passed for
        that encoder                     → the encoder.

    The gate is ``env.gpu.functional`` — a live encode test run by the
    env probe — NOT the compiled-in ``ffmpeg -encoders`` list, because a
    ffmpeg build can advertise a hardware encoder the installed driver
    is too old to open. Pure function; no I/O. Safe to call from the UI
    thread for a pre-flight status line.
    """
    if engine not in ("auto", "gpu"):
        return (None, None)
    profile = selected_gpu_profile(env)
    if profile is not None:
        family = getattr(video_codec, "gpu_family", "") or ""
        gpu_enc = profile.encoders.get(family)
        api = profile.api
    else:
        # No GPU profile context (older callers / no probe data): fall
        # back to the profile's NVENC encoder.
        gpu_enc = getattr(video_codec, "gpu_encoder", "") or ""
        api = "nvenc" if gpu_enc else None
    if not gpu_enc:
        return (None, None)
    gpu_info = getattr(env, "gpu", None)
    if gpu_info is not None and gpu_info.functional.get(gpu_enc, False):
        return (gpu_enc, api)
    return (None, None)




class EncoderWorker(QThread):
    log_msg       = Signal(str)
    progress_msg  = Signal(str, int, int)   # (filename, current, total)
    finished_queue = Signal(int, int)        # (success_count, fail_count)

    # v4.4.3/v4.6.0: class-level defaults for attributes normally set in
    # __init__. The mocked test suite builds workers via ``__new__``
    # (bypassing __init__) and calls non-Qt methods directly; without
    # these defaults those instances crash with AttributeError on
    # ``verbose`` / ``_current_total`` (the "AttributeError: no attribute
    # 'verbose'" class of bugs from the v4.4.3 changelog). Instance
    # assignment in __init__ shadows these harmlessly.
    verbose = False
    _current_idx = 0
    _current_total = 0
    _current_filename = ""

    def __init__(
        self,
        in_dir: Path,
        out_dir: Path,
        video_codec: VideoCodecProfile,
        audio_profile: AudioProfile,
        container: ContainerProfile,
        crf: int,
        preset_label: str,
        delete_source: bool,
        env: EnvProbe,
        extensions: set[str],
        resolution: ResolutionProfile,
        audio_level_db: float = 0.0,
        use_ffmpeg_fallback: bool = False,
        subtitle_lang: str | None = None,
        force: bool = False,
        # v4.1.0: explicit overrides for the intelligent worker-count
        # computation. When None, EncoderWorker computes (worker_count,
        # threads_per_worker) from CPU topology so that
        # ``worker_count * threads_per_worker <= logical_threads - 1``
        # (i.e. no thread oversubscription → no hard lock). When set,
        # these take precedence — useful for troubleshooting or for
        # workloads where the auto-compute picks a suboptimal split.
        # Both can also be supplied via env.av1an_flags["max_workers"] /
        # ["threads_per_worker"] (set by the CLI's --max-workers /
        # --threads-per-worker flags) so the GUI doesn't need code changes
        # to honor them.
        max_workers: int | None = None,
        threads_per_worker: int | None = None,
        # v4.6.0: encode engine selection. "auto" uses the NVENC GPU
        # encoder when the selected codec family has one AND the env
        # probe's live encode test proved it works on this system;
        # otherwise (or with "cpu") the CPU encoders are used. "gpu"
        # requests GPU and falls back to CPU with a log line when the
        # hardware is unavailable. Resolved against env.av1an_flags
        # ["engine"] like the other CLI-plumbed flags.
        engine: str | None = None,
        # v4.7.0: hybrid-lane support. *file_subset* restricts this
        # worker to an explicit file list (the hybrid scheduler scans and
        # partitions the queue up front, then spawns a GPU-lane and a
        # CPU-lane worker with disjoint subsets). *lane* suffixes the
        # per-worker temp dir so one lane's cleanup sweep can never
        # delete the other lane's intermediates. *ffmpeg_threads* caps
        # the CPU lane's software ffmpeg encode (GPU jobs are capped by
        # NVENC silicon, not threads).
        file_subset: list[Path] | None = None,
        lane: str = "",
        ffmpeg_threads: int | None = None,
    ):


        super().__init__()
        self.in_dir = in_dir
        self.out_dir = out_dir
        self.video_codec = video_codec
        self.audio_profile = audio_profile
        self.container = container
        self.crf = crf
        self.preset_val = video_codec.preset_map.get(preset_label, 6)
        self.delete_source = delete_source
        self.env = env
        self.extensions = extensions
        self.resolution = resolution
        self.audio_level_db = audio_level_db
        self.use_ffmpeg_fallback = use_ffmpeg_fallback
        self.subtitle_lang = subtitle_lang
        # v5: force=True skips ffprobe validation and attempts encode even
        # for files ffprobe cannot read. Use for the 1% edge case where
        # ffprobe fails but the file is actually valid (rare codec, broken
        # container metadata, etc.). Default False — most "ffprobe can't
        # read" files are genuinely invalid (failed downloads, HTML saved
        # as .mp4, truncated files, etc.).
        self.force = force
        # v4.1.0: intelligent chunking overrides. Falls back to
        # env.av1an_flags if not explicitly passed (so the CLI flags
        # --max-workers / --threads-per-worker reach the GUI-spawned
        # worker without ui_window.py code changes).
        self.max_workers = max_workers if max_workers is not None else (
            env.av1an_flags.get("max_workers") if isinstance(
                env.av1an_flags.get("max_workers"), int
            ) else None
        )
        self.threads_per_worker_override = (
            threads_per_worker if threads_per_worker is not None else (
                env.av1an_flags.get("threads_per_worker") if isinstance(
                    env.av1an_flags.get("threads_per_worker"), int
                ) else None
            )
        )
        # v4.6.0: engine selection ("auto" | "gpu" | "cpu"). Falls back to
        # env.av1an_flags["engine"] when not passed explicitly (same
        # pattern as max_workers — lets the CLI reach the GUI-spawned
        # worker without ui_window changes). Resolved to a concrete
        # GPU/CPU decision in run() via resolve_gpu_encoder().
        self.engine = engine if engine in ("auto", "gpu", "cpu") else (
            env.av1an_flags.get("engine", "auto")
            if env.av1an_flags.get("engine") in ("auto", "gpu", "cpu")
            else "auto"
        )
        # Resolved in run(): NVENC encoder name when the GPU path is
        # active, None for CPU. _ffmpeg_fallback_encode and
        # _prepare_input read this (GPU mode implies the ffmpeg path —
        # av1an cannot drive NVENC — which also means no pre-scale
        # intermediate: the ffmpeg path scales inline).
        self._gpu_encoder: str | None = None
        # Resolved at run() time — kept on self so _encode_one can read it
        # without changing its call signature (which is invoked recursively
        # by the y4m-pipe-break retry path).
        self._resolved_threads_per_worker = 0
        self._stop = False
        self._current_temps: list[Path] = []   # temps for the file currently being processed
        self._sources_to_delete: list[Path] = []  # sources deferred for deletion after final cleanup
        self.success_count = 0
        self.fail_count = 0
        # v4.3.0: tracks files skipped because the output already existed
        # with a matching codec. Reported in the final summary as
        # "Skipped: N" alongside Success/Failed.
        self.skipped_count = 0
        # v4.3.0: skip-existing detection. When True (default), the
        # worker probes the output file before encoding; if it already
        # exists with a matching video+audio codec, the file is skipped.
        self.skip_existing = bool(env.av1an_flags.get("skip_existing", True))
        # v4.4.3: verbose flag (was missing in launcher script, causing
        # AttributeError when _check_disk_space referenced self.verbose).
        self.verbose = bool(env.av1an_flags.get("verbose", False))
        # v4.4.0: per-file encode timeout (seconds). Default 86400s = 24h,
        # up from v4.0.0's 7200s = 2h. A 30GB 1080p BluRay rip at SVT-AV1
        # preset 6 (~5-10 fps) on a 2-hour movie takes 4-10 hours; the old
        # 2h timeout killed massive-file encodes partway through.
        self.encode_timeout = int(env.av1an_flags.get("encode_timeout", 86400))
        # v4.4.4: inline_scale — when True and a target resolution is
        # selected, the scale filter is passed directly to av1an via
        # --ffmpeg-filter-args instead of pre-scaling to a CRF-16
        # intermediate. This skips the extra encode pass entirely (no
        # intermediate file → no disk space wasted) and is significantly
        # faster. The intermediate path remains the default because it
        # works with every chunk method and is robust against av1an/
        # VapourSynth filter-arg quirks on older builds. Toggle via the
        # "Inline scale (no intermediate)" UI checkbox or --inline-scale.
        self.inline_scale = bool(env.av1an_flags.get("inline_scale", False))
        # Stashed per-file by _process_one_file so _encode_one can inject
        # the scale filter into av1an's --ffmpeg-filter-args without
        # changing its (recursively-called) signature.
        self._current_scale_filter: str = ""
        # v5-02: track consecutive failures with the same error pattern.
        # After 3 consecutive same-pattern failures, auto-abort the queue.
        self._consecutive_fail_count = 0
        self._last_fail_pattern: str | None = None
        # v3 (OTC-013, SEI CERT FIO09-C): each worker gets its own
        # per-PID subdir under the shared app temp dir, so the final
        # cleanup sweep can safely nuke only this worker's intermediates
        # without affecting a concurrent worker. The subdir is created
        # with mode=0o700 to prevent symlink attacks from other users.
        self.file_subset = file_subset
        self.lane = lane
        self.ffmpeg_threads = ffmpeg_threads
        self._temp_dir = _worker_temp_dir(os.getpid(), lane=lane)


        # v6-06: KeepAwake instance — started in run(), stopped in finally.
        # mouse_nudge defaults to False (opt-in) to avoid surprising the
        # user with cursor movement. systemd-inhibit is always-on when
        # available (no visible side effects).
        self._keepawake = KeepAwake(
            log_fn=lambda msg: self.log_msg.emit(msg),
            enable_mouse_nudge=False,
        )
        self._encode_start_time = 0.0
        # v4.4.0: per-file context for combined status lines.
        self._current_idx = 0
        self._current_total = 0
        self._current_filename = ""

    def _status_prefix(self) -> str:
        """v4.4.0: Build the '[N/total] filename — ' prefix for combined status lines."""
        if self._current_total:
            return f"[{self._current_idx}/{self._current_total}] {self._current_filename} — "
        return f"{self._current_filename} — " if self._current_filename else ""

    def _vlog(self, msg: str) -> None:
        """v4.2.1: Verbose-only log emit. No-op unless self.verbose is True."""
        if self.verbose:
            self.log_msg.emit(msg)

    def _compute_intelligent_worker_count(self) -> tuple[int, int]:
        """Compute ``(worker_count, threads_per_worker)`` to prevent thread
        oversubscription on high-core-count machines. Delegates to the
        module-level ``_compute_intelligent_worker_count_for`` so the GUI
        probe banner can call the same math without an EncoderWorker
        instance. See the module-level docstring for the full algorithm.
        """
        return _compute_intelligent_worker_count_for(
            self.env,
            max_workers=self.max_workers,
            threads_per_worker_override=self.threads_per_worker_override,
        )

    def _run_with_stop_check(
        self,
        cmd: list[str],
        env: dict[str, str] | None = None,
        timeout: int = 7200,
        log_prefix: str = "  ",
    ) -> tuple[str, int, str, str]:
        """Run a subprocess with STOP-button support.

        Replaces ``subprocess.run(cmd, capture_output=True, text=True,
        timeout=7200)`` in the av1an and ffmpeg-fallback encode paths so
        that clicking STOP in the UI interrupts a running encode within
        ~1 second instead of waiting up to 2 hours for the per-file
        timeout to expire.

        Polls ``self._stop`` every ~1 second.  When STOP is requested,
        sends SIGTERM to the subprocess's *process group* (so av1an's
        child encoders — SvtAv1EncApp / vpxenc / x265 — die too, not just
        the av1an parent), waits 5s, then SIGKILLs the group if still
        alive.  Also enforces the overall ``timeout`` (7200s) limit.

        Two background drainer threads read stdout/stderr continuously
        into StringIO buffers.  This prevents the classic pipe-buffer
        deadlock: av1an's progress bar can easily exceed the ~64KB OS
        pipe buffer over a long encode, and without draining the child
        would block on ``write()`` and ``proc.poll()`` would never see
        it exit.  This is the same pattern ``subprocess.run`` uses
        internally via ``_communicate``.

        Returns a 4-tuple ``(status, returncode, stdout, stderr)`` where
        ``status`` is one of:

          - ``"ok"``      — process exited normally; caller inspects
                            ``returncode`` (0 = success) and uses
                            ``stdout`` / ``stderr`` for diagnostics.
          - ``"stop"``    — user requested STOP via the UI.  Caller must
                            NOT increment ``fail_count`` (a user abort is
                            not a transcode failure).  ``self._stop`` is
                            already True (set by the UI thread), so the
                            orchestrator's queue loop will break on the
                            next iteration and emit
                            "STOP: Aborted by user."
          - ``"timeout"`` — process exceeded ``timeout`` seconds.
                            Caller MUST increment ``fail_count`` (a
                            timeout is a failure) and emit the existing
                            user-visible TIMEOUT message.

        Raises ``OSError`` / ``subprocess.SubprocessError`` if the
        ``Popen`` constructor itself fails (e.g. ``FileNotFoundError``
        when the binary is missing) — the caller's existing ``except``
        clause handles these unchanged.
        """
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            # start_new_session=True puts the child in its own process
            # group (setsid).  We can then os.killpg() the whole group
            # to reach av1an's child encoders (SvtAv1EncApp / vpxenc /
            # x265), which a bare proc.terminate() would miss.
            start_new_session=True,
        )

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()

        # v4.1.1: live tail — emit each line of av1an's stdout/stderr
        # to the GUI log as it arrives, so the user sees progress in
        # real-time instead of staring at a frozen "Encoding: file.mp4"
        # message for 10+ minutes. The previous drainer read into a
        # StringIO buffer and only emitted on process exit, which made
        # v4.1.0's slower (capped-thread) encodes look "borked" even
        # though av1an was working fine underneath.
        #
        # Handles both \n (log lines) and \r (progress bar updates) as
        # line boundaries, so av1an's progress bar renders correctly.
        # Incomplete trailing data is buffered until the next read
        # completes the line.
        def _drain(stream, buf, emit_fn, prefix):
            """Read from stream into buf, emitting each complete line via
            emit_fn. Handles \\n and \\r as line boundaries."""
            pending = ""
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        break
                    buf.write(chunk)
                    if emit_fn is None:
                        continue
                    pending += chunk
                    # Emit each complete line (delimited by \n or \r).
                    # av1an's progress bar uses \r; log lines use \n.
                    while True:
                        nl = pending.find('\n')
                        cr = pending.find('\r')
                        if nl == -1 and cr == -1:
                            break
                        if nl == -1:
                            pos = cr
                        elif cr == -1:
                            pos = nl
                        else:
                            pos = min(nl, cr)
                        line = pending[:pos]
                        pending = pending[pos + 1:]
                        stripped = line.rstrip()
                        if stripped:
                            try:
                                emit_fn(f"{prefix}{stripped}")
                            except (RuntimeError, OSError):
                                # Signal might be disconnected mid-encode
                                # if the GUI is closing. Stop emitting
                                # but keep draining the buffer.
                                emit_fn = None
                                break
                    if emit_fn is None:
                        break
            except (OSError, ValueError):
                # Stream closed under us or process gone — stop reading.
                pass
            # Emit any remaining pending data (process exited mid-line).
            if emit_fn is not None:
                stripped = pending.rstrip()
                if stripped:
                    try:
                        emit_fn(f"{prefix}{stripped}")
                    except (RuntimeError, OSError):
                        pass

        tail_prefix = f"{log_prefix}│ "
        # v4.4.2: gate live tail behind --verbose. The user wants just
        # start + finish lines, no per-frame progress chatter.
        tail_emit = self.log_msg.emit if self.verbose else None
        t_out = threading.Thread(
            target=_drain,
            args=(proc.stdout, stdout_buf, tail_emit, tail_prefix),
            daemon=True,
        )
        t_err = threading.Thread(
            target=_drain,
            args=(proc.stderr, stderr_buf, tail_emit, tail_prefix),
            daemon=True,
        )
        t_out.start()
        t_err.start()

        status = "ok"
        rc: int | None = None
        start_time = time.monotonic()
        # v4.1.1: heartbeat timer — emit a "still encoding" message every
        # 30 seconds so the user knows the process is alive even if av1an
        # isn't producing line-delimited output (e.g. during a long SVT-AV1
        # encode that only updates a \r progress bar, which the live tail
        # emits as a single line that might not change for minutes).
        last_heartbeat = start_time
        HEARTBEAT_INTERVAL = 30  # seconds
        while True:
            rc = proc.poll()
            if rc is not None:
                # Process exited — break and drain pipes below.
                break

            if self._stop:
                self.log_msg.emit(
                    f"{log_prefix}STOP: Aborting current encode, "
                    f"terminating subprocess..."
                )
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    # Process already gone — nothing to signal.
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # SIGTERM didn't take effect within the grace period —
                    # escalate to SIGKILL on the whole group.
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except (ProcessLookupError, OSError):
                        pass
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        # Truly stuck (e.g. uninterruptible IO).  We've
                        # done what we can; the process will be reaped
                        # later.  Continue to pipe drainage.
                        pass
                status = "stop"
                self.log_msg.emit(f"{log_prefix}STOP: Subprocess terminated.")
                break

            if time.monotonic() - start_time > timeout:
                # Overall timeout — kill the process group.  The caller
                # logs the user-visible TIMEOUT message (it includes the
                # file name / "ffmpeg" context this helper doesn't know).
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                status = "timeout"
                break

            # v4.4.1: heartbeat gated behind --verbose. The user wants
            # just start + finish lines — no "still encoding" chatter
            # in between.
            now = time.monotonic()
            if self.verbose and now - last_heartbeat >= HEARTBEAT_INTERVAL:
                elapsed = int(now - start_time)
                self.log_msg.emit(
                    f"{log_prefix}... {elapsed}s elapsed"
                )
                last_heartbeat = now

            time.sleep(1)

        # Wait for drainer threads to finish reading any remaining pipe
        # data, then close the pipes explicitly (defensive — __del__
        # would also close them, but explicit is better and avoids
        # ResourceWarning under -X dev).
        t_out.join(timeout=10)
        t_err.join(timeout=10)
        try:
            proc.stdout.close()
        except (OSError, ValueError):
            pass
        try:
            proc.stderr.close()
        except (OSError, ValueError):
            pass

        return (
            status,
            rc if rc is not None else -1,
            stdout_buf.getvalue(),
            stderr_buf.getvalue(),
        )

    def _ffmpeg_fallback_encode(
        self,
        file_path: Path,
        encode_input: Path,
        output_f: Path,
    ) -> bool:
        """Encode a single file using pure ffmpeg (av1an fallback path).

        Used when av1an cannot initialize VapourSynth.  No chunk-parallel
        mode, but ffmpeg uses multithreaded encoding internally.

        Returns True on success, False on failure.
        """
        # Check if ffmpeg has the video encoder we need
        # v4.6.0: GPU mode swaps in the hardware encoder and its vargs.
        # v4.8.0: VAAPI/QSV APIs build their command shape (device init,
        # hwupload filter, quality args) from gpu_profiles; NVENC keeps
        # the original profile vargs.
        ffmpeg_enc = self._gpu_encoder or self.video_codec.ffmpeg_encoder
        v_args = (
            (self.video_codec.gpu_vargs_fn or self.video_codec.ffmpeg_vargs_fn)
            if self._gpu_encoder else self.video_codec.ffmpeg_vargs_fn
        )(self.crf, self.preset_val)
        hw_pre_args: list[str] = []
        hw_filter_args: list[str] = []
        if self._gpu_encoder and self._gpu_api in ("vaapi", "qsv"):
            profile = selected_gpu_profile(self.env)
            if profile is not None:
                hw_pre_args = encoder_pre_args(profile)
                hw_filter_args = encoder_filter_chain(profile)
                v_args = encoder_quality_args(
                    self._gpu_api, ffmpeg_enc, self.crf, self.preset_val
                )
        # v3: use the module-level FFMPEG_LIB_KEY_MAP (OTC-007).
        ffmpeg_lib_key = ffmpeg_lib_key_for(ffmpeg_enc)

        if not self.env.ffmpeg_libs.get(ffmpeg_lib_key, False):
            self.log_msg.emit(
                f"  FATAL: ffmpeg does not have '{ffmpeg_enc}' encoder. "
                f"Cannot fall back. Install a ffmpeg build with {ffmpeg_enc} support."
            )
            return False

        # Belt-and-suspenders: if a target resolution is set, inject -vf scale
        # directly into the ffmpeg command. This guarantees the output resolution
        # matches the dropdown even if the intermediate pre-scale was bypassed.
        vf_scale_args: list[str] = []
        if self.resolution.width is not None and self.resolution.height is not None:
            if self._gpu_api == "vaapi":
                # v4.8.0: VAAPI scales ON the hardware — combine the
                # scalar with the hwupload upload in one chain.
                vf_scale_args = [
                    "-vf", (
                        f"scale={self.resolution.width}:{self.resolution.height}:"
                        f"force_original_aspect_ratio=decrease:force_divisible_by=2,"
                        f"format=nv12,hwupload"
                    ),
                ]
                hw_filter_args = []
            else:
                vf_scale_args = [
                    "-vf", (
                        f"scale={self.resolution.width}:{self.resolution.height}:"
                        f"force_original_aspect_ratio=decrease:force_divisible_by=2"
                    ),
                ]

        # Audio args from profile
        audio_args = list(self.audio_profile.params)
        if abs(self.audio_level_db) > 0.01:
            per_file_gain = self._analyze_audio_loudness(file_path)
            if per_file_gain is not None and abs(per_file_gain) > 0.01:
                audio_args.extend(["-af", f"volume={per_file_gain:+.1f}dB"])
            else:
                static_db = f"{self.audio_level_db:+.1f}".replace("+", "")
                audio_args.extend(["-af", f"volume={static_db}dB"])

        # Container-specific muxer flags. -movflags +faststart is MP4-only
        # (it relocates the moov atom for streaming); passing it for MKV or
        # WebM is silently ignored by ffmpeg but pollutes the command line
        # and confuses users reading the log. Apply it only when the
        # output container is MP4.
        mux_flags: list[str] = []
        if self.container.ext == "mp4":
            mux_flags = ["-movflags", "+faststart"]

        cmd = [self.env.ffmpeg_path] + hw_pre_args + [
            "-i", str(encode_input),
        ] + vf_scale_args + hw_filter_args + v_args
        # v4.7.0: CPU lane thread cap in hybrid mode (the GPU lane's
        # NVENC job keeps ~2 threads for decode/mux). Never applied on
        # the GPU path — NVENC throughput is silicon-bound, not
        # thread-bound. Skipped when the cap is 0/unset.
        if self._gpu_encoder is None and self.ffmpeg_threads:
            # v4.7.1: libx265 maps -threads to frame-threads, capped at
            # X265_MAX_FRAME_THREADS (16) — larger values abort the
            # encoder ("frameNumThreads must be [0 .. X265_MAX_FRAME_
            # THREADS)"). SVT-AV1 and libvpx accept the full budget.
            threads = self.ffmpeg_threads
            if ffmpeg_enc == "libx265":
                threads = min(threads, 16)
            cmd += ["-threads", str(threads)]

        # v4.7.0: CPU lane thread cap in hybrid mode (the GPU lane's
        # NVENC job keeps ~2 threads for decode/mux). Never applied on
        # the GPU path — NVENC throughput is silicon-bound, not
        # thread-bound. Skipped when the cap is 0/unset.
        if self._gpu_encoder is None and self.ffmpeg_threads:
            # v4.7.1: libx265 maps -threads to frame-threads, capped at
            # X265_MAX_FRAME_THREADS (16) — larger values abort the
            # encoder ("frameNumThreads must be [0 .. X265_MAX_FRAME_
            # THREADS)"). SVT-AV1 and libvpx accept the full budget.
            threads = self.ffmpeg_threads
            if ffmpeg_enc == "libx265":
                threads = min(threads, 16)
            cmd += ["-threads", str(threads)]
        cmd += audio_args + mux_flags + [
            "-y",
            str(output_f),
        ]

        try:
            result = self._run_with_stop_check(cmd, timeout=self.encode_timeout, log_prefix="  ")
            status, rc, stdout, stderr = result

            if status == "stop":
                # User requested STOP — do NOT count as failure.  The
                # caller (_process_one_file) guards the fail_count
                # increment with `if not self._stop`.  Remove partial
                # output so it isn't mistaken for a finished file.
                output_f.unlink(missing_ok=True)
                return False
            if status == "timeout":
                self.log_msg.emit(f"{self._status_prefix()}FAIL: timeout (exceeded {self.encode_timeout}s limit)")
                return False

            # status == "ok" — wrap in CompletedProcess so the downstream
            # returncode/stderr logic is byte-for-byte unchanged.
            res = subprocess.CompletedProcess(cmd, rc, stdout, stderr)

            if res.returncode == 0 and output_f.exists():
                src_size = file_path.stat().st_size
                out_size = output_f.stat().st_size
                ratio = out_size / src_size if src_size > 0 else 0

                # Integrity gate: 1KB absolute minimum. A valid container
                # header alone is ~1KB; anything below is definitely corrupt.
                # The duration check in _verify_and_finalize (≥95% of source
                # duration) is the real quality gate for high-bitrate sources.
                if out_size > 1024:
                    return True
                else:
                    self.log_msg.emit(
                        f"  INTEGRITY: output only {ratio * 100:.1f}% of source."
                    )
                    output_f.unlink(missing_ok=True)
                    return False
            else:
                stderr_snip = (res.stderr or "")[-300:]
                self.log_msg.emit(
                    f"  ffmpeg error (rc={res.returncode}): {stderr_snip.strip()}"
                )
                # v4.7.1: remove the partial output. Without this, a
                # failed encode left a truncated file that ffprobe can
                # still parse as the right codec — and skip-existing
                # would then treat it as a finished archive forever.
                output_f.unlink(missing_ok=True)
                return False
        except OSError as e:
            self.log_msg.emit(f"{self._status_prefix()}FAIL: system error: {e}")
            return False

    def run(self):
        # v4.1.0: intelligent worker count + per-chunk thread cap.
        # Replaces the v3 ``max(1, physical_cores - 1)`` heuristic that
        # produced 13 workers × auto (≈28) = 364 threads on a 28-thread
        # Xeon and drowned the kernel scheduler (hard lock).
        # _compute_intelligent_worker_count returns (worker_count,
        # threads_per_worker) such that
        #   worker_count * threads_per_worker <= logical_threads - 1
        # The threads_per_worker is stashed on self so _encode_one can
        # inject it into the encoder's --video-params (each SvtAv1EncApp
        # / vpxenc / x265 instance then respects its share).
        worker_count, threads_per_worker = self._compute_intelligent_worker_count()
        self._resolved_threads_per_worker = threads_per_worker
        phys = self.env.cpu.physical_cores
        logical = self.env.cpu.logical_threads

        # ── v4.6.0: engine resolution (GPU vs CPU) ──
        # GPU mode is a variant of the ffmpeg path: av1an invokes
        # encoder CLI binaries (SvtAv1EncApp / vpxenc / x265) and cannot
        # drive NVENC, so an active GPU encoder forces the single-pass
        # ffmpeg path. NVENC on even a GTX 1070 encodes 1080p at several
        # hundred fps — one ffmpeg process beats av1an's chunk-parallel
        # CPU workers, and chunking becomes unnecessary.
        self._gpu_encoder, self._gpu_api = resolve_gpu_encoder(
            self.engine, self.video_codec, self.env)
        if self._gpu_encoder:
            self.use_ffmpeg_fallback = True
            self.log_msg.emit(
                f"ENGINE: GPU ({self._gpu_encoder}, {self._gpu_api}) — "
                f"single-pass ffmpeg hardware encode; av1an chunk-parallel "
                f"not used."
            )
        elif self.engine == "gpu":
            gpu_enc = getattr(self.video_codec, "gpu_encoder", "") or ""
            if not gpu_enc and not getattr(self.video_codec, "gpu_family", ""):
                self.log_msg.emit(
                    f"ENGINE: GPU requested but {self.video_codec.label} has no "
                    f"hardware encoder — using CPU."
                )
            else:
                gpu = getattr(self.env, "gpu", None)
                detail = gpu.first_failure_detail if gpu is not None else ""
                self.log_msg.emit(
                    f"ENGINE: GPU requested but no usable hardware encoder for "
                    f"{self.video_codec.label} ({detail or 'unavailable'}) — using CPU."
                )

        # Collect all valid files first (for progress tracking)
        # Exclude our own temp intermediates from previous failed runs.
        if self.file_subset is not None:
            # v4.7.0: hybrid lane — the scheduler partitioned the queue.
            all_files = sorted(self.file_subset)
        else:
            all_files = scan_input_files(self.in_dir, self.extensions)

        total = len(all_files)

        if total == 0:
            self.log_msg.emit("INFO: No matching files found in source directory.")
            self.finished_queue.emit(0, 0)
            return

        # ── Mode banner ──
        # v4.2.1: mode banner is verbose-only. The user doesn't need
        # to know the worker math — they just need files to encode.
        # use_ffmpeg_fallback is set by the main thread's pre-flight check.
        if self.verbose:
            if self._gpu_encoder:
                self._vlog(
                    f"GPU encode: {self.video_codec.label} via "
                    f"{self._gpu_encoder} (NVENC), CPU decode"
                )
            elif self.use_ffmpeg_fallback:
                self._vlog(
                    f"FFmpeg fallback: {self.video_codec.ffmpeg_encoder} on {phys} cores "
                    f"(single-pass, no chunk-parallel)"
                )
            else:
                # v4.1.0: show the thread budget so the user can verify the
                # intelligent worker math at a glance. e.g. on a 28-thread Xeon:
                #   "Chunk-parallel: 6 workers × 4 threads = 24 active
                #    (28 logical - 4 reserved for OS/UI)"
                active = worker_count * threads_per_worker
                reserved = logical - active
                self._vlog(
                    f"Chunk-parallel: {worker_count} workers × {threads_per_worker} threads "
                    f"= {active} active "
                    f"({logical} logical - {reserved} reserved for OS/UI)"
                )
                if self.max_workers is not None or self.threads_per_worker_override is not None:
                    self._vlog(
                        f"  (overrides: max_workers={self.max_workers!r}, "
                        f"threads_per_worker={self.threads_per_worker_override!r})"
                    )
        self.log_msg.emit(f"Found {total} file(s) to process.")
        self.log_msg.emit(f"Temp dir: {self._temp_dir}")

        # ── Pre-scan: show each file's source → output resolution ──
        needs_scale = (
            self.resolution.width is not None
            and self.resolution.height is not None
        )
        if needs_scale:
            self.log_msg.emit(f"Output resolution: {self.resolution.width}x{self.resolution.height} ({self.resolution.aspect_label})")
        else:
            self.log_msg.emit("Output resolution: Original (no scaling)")
        self.log_msg.emit("─── FILE RESOLUTION MAP ───")
        self._file_res_map: dict[Path, tuple] = {}  # file -> (src_w, src_h, out_w, out_h)
        if self.env.ffprobe_path:
            for f in all_files:
                info = ffprobe_validate(f, self.env.ffprobe_path)
                sw, sh = None, None
                if info:
                    for s in info.get("streams", []):
                        if s.get("codec_type") == "video":
                            sw = int(s.get("width", 0) or 0)
                            sh = int(s.get("height", 0) or 0)
                            break
                if sw and sh:
                    ow, oh = (self.resolution.width, self.resolution.height) if needs_scale else (sw, sh)
                    self._file_res_map[f] = (sw, sh, ow, oh)
                    arrow = "->" if needs_scale else "="
                    action = "" if needs_scale or sw == ow else " (no change)"
                    self.log_msg.emit(f"  {f.name:<40s} {sw:>5}x{sh:<5} {arrow} {ow:>5}x{oh}{action}")
                else:
                    self._file_res_map[f] = (None, None, self.resolution.width if needs_scale else None, self.resolution.height if needs_scale else None)
                    self.log_msg.emit(f"  {f.name:<40s} (unknown resolution)")
        else:
            self.log_msg.emit("  (ffprobe unavailable — resolution map skipped)")
        self.log_msg.emit("───────────────────────────")

        # ── v5-04: Pre-flight validation pass ──
        # Scan all files with ffprobe BEFORE the encode loop. Report how
        # many are valid vs invalid. This gives the user immediate feedback
        # ("46 files found, 0 valid, 46 invalid") instead of failing one
        # by one over 2 hours. If ALL files are invalid and force=False,
        # abort now — don't waste time entering the encode loop.
        if self.env.ffprobe_path and not self.force:
            valid_count = 0
            invalid_count = 0
            invalid_samples: list[str] = []
            for f in all_files:
                info = ffprobe_validate(f, self.env.ffprobe_path)
                if info is None:
                    invalid_count += 1
                    if len(invalid_samples) < 3:
                        ft = _identify_file_type(f)
                        invalid_samples.append(f"  {f.name}: {ft}" if ft else f"  {f.name}: (file type unknown)")
                else:
                    has_video = any(s.get("codec_type") == "video" for s in info.get("streams", []))
                    duration = float(info.get("format", {}).get("duration", 0))
                    if has_video and duration >= 0.5:
                        valid_count += 1
                    else:
                        invalid_count += 1
                        if len(invalid_samples) < 3:
                            reason = "no video stream" if not has_video else f"too short ({duration:.1f}s)"
                            invalid_samples.append(f"  {f.name}: {reason}")

            self.log_msg.emit("─── PRE-FLIGHT VALIDATION ───")
            self.log_msg.emit(f"  Valid files:   {valid_count}")
            self.log_msg.emit(f"  Invalid files: {invalid_count}")
            if invalid_samples:
                self.log_msg.emit(f"  First {len(invalid_samples)} invalid:")
                for s in invalid_samples:
                    self.log_msg.emit(s)
            self.log_msg.emit("─────────────────────────────")

            if valid_count == 0 and invalid_count > 0:
                self.log_msg.emit("")
                self.log_msg.emit(
                    f"ABORT: All {invalid_count} file(s) are invalid. "
                    f"Aborting queue — no files to encode."
                )
                self.log_msg.emit(
                    "  Common causes: (1) failed yt-dlp downloads (HTML saved as .mp4), "
                    "(2) files on a network mount that's not responding, "
                    "(3) wrong input directory."
                )
                self.log_msg.emit(
                    "  Run `file <filename>` on any file to see what it actually is."
                )
                self.fail_count = invalid_count
                self._final_cleanup_sweep()
                self.log_msg.emit(
                    f"QUEUE COMPLETE. Success: 0, Failed: {self.fail_count}."
                )
                self.finished_queue.emit(0, self.fail_count)
                return
            elif invalid_count > 0:
                self.log_msg.emit(
                    f"  {invalid_count} invalid file(s) will be skipped during encoding."
                )
                self.log_msg.emit("")

        scale_filter = (
            f"scale={self.resolution.width}:{self.resolution.height}:"
            f"force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"pad={self.resolution.width}:{self.resolution.height}:(ow-iw)/2:(oh-ih)/2"
        ) if needs_scale else ""

        # v6-06: Start keep-awake (systemd-inhibit + optional mouse nudge)
        self._encode_start_time = time.monotonic()
        self._keepawake.start()
        try:
            for idx, file_path in enumerate(all_files, 1):
                if self._stop:
                    self.log_msg.emit("STOP: Aborted by user.")
                    break

                prev_success = self.success_count
                prev_fail = self.fail_count

                # v6-06: Update keep-awake ETA before each file.
                # ETA = (avg time per file so far) × (remaining files)
                processed = idx - 1
                if processed > 0:
                    elapsed = time.monotonic() - self._encode_start_time
                    avg_per_file = elapsed / processed
                    remaining = total - processed
                    self._keepawake.update_eta(avg_per_file * remaining)
                else:
                    self._keepawake.update_eta(None)  # unknown for first file

                self._process_one_file(file_path, idx, total, worker_count, needs_scale, scale_filter)

                # v5-02: track consecutive failures with the same error pattern.
                # After 3 consecutive same-pattern failures, auto-abort the queue.
                if self.fail_count > prev_fail:
                    pass  # pattern tracking is handled in _process_one_file
                elif self.success_count > prev_success:
                    # Success resets the consecutive failure counter.
                    self._consecutive_fail_count = 0
                    self._last_fail_pattern = None

            # ── Final cleanup pass: residual sweep ──
            self._final_cleanup_sweep()

            # ── Deferred source deletion (only after all cleanup is done) ──
            if self._sources_to_delete:
                deleted = 0
                for src in self._sources_to_delete:
                    try:
                        if src.exists():
                            src.unlink()
                            deleted += 1
                    except OSError:
                        pass
                self.log_msg.emit(f"CLEANED: Removed {deleted} source file(s) after verified transcode.")
                self._sources_to_delete.clear()

            # v4.3.0: include skipped count when > 0.
            if self.skipped_count > 0:
                self.log_msg.emit(
                    f"QUEUE COMPLETE. Success: {self.success_count}, "
                    f"Failed: {self.fail_count}, Skipped: {self.skipped_count}."
                )
            else:
                self.log_msg.emit(
                    f"QUEUE COMPLETE. Success: {self.success_count}, Failed: {self.fail_count}."
                )
            self.finished_queue.emit(self.success_count, self.fail_count)
        finally:
            # v6-06: Always stop keep-awake, even if the encode loop crashed.
            self._keepawake.stop()

    def _process_one_file(self, file_path, idx, total, worker_count, needs_scale, scale_filter):
        """Process a single file end-to-end (validate -> prepare -> encode -> verify).

        v4.4.0: the per-file banner is NOT emitted upfront. Instead, each
        terminal status (SKIP / OK / FAIL) emits a SINGLE combined line:
          [N/total] filename — SKIP (already av1/opus)
          [N/total] filename — OK: 1.6MB -> 1.3MB (81%)
          [N/total] filename — FAIL: <reason>
        """
        self.progress_msg.emit(file_path.name, idx, total)
        # v4.4.0: stash idx/total on self so downstream methods can emit
        # combined status lines with the [N/total] filename prefix.
        self._current_idx = idx
        self._current_total = total
        self._current_filename = file_path.name
        # v4.4.4: stash scale_filter so _encode_one can inject it into
        # av1an's --ffmpeg-filter-args when self.inline_scale is True.
        self._current_scale_filter = scale_filter or ""

        # --- ffprobe pre-validation ---
        skip, info, src_w, src_h = self._validate_file(file_path)
        if skip:
            # v5-02: a skip is a failure for consecutive-failure tracking.
            self._check_consecutive_failures(file_path, accepted=False)
            return  # _validate_file already logged SKIP + incremented fail_count

        # --- Determine actual output resolution ---
        if src_w and src_h:
            out_w, out_h = src_w, src_h
            if needs_scale:
                out_w, out_h = self.resolution.width, self.resolution.height
            self.log_msg.emit(f"  Source: {src_w}x{src_h}  ->  Output: {out_w}x{out_h}")
        else:
            if needs_scale:
                self.log_msg.emit(f"  Source: unknown  ->  Output: {self.resolution.width}x{self.resolution.height}")
            else:
                self.log_msg.emit(f"  Source: unknown  ->  Output: original")

        # --- Pre-scale / symlink + build output path ---
        prepared = self._prepare_input(file_path, src_w, src_h, needs_scale, scale_filter)
        if prepared is None:
            # v5-02: prepare failure counts for consecutive-failure tracking.
            self._check_consecutive_failures(file_path, accepted=False)
            return  # _prepare_input already logged + cleaned up + incremented fail_count
        encode_input, output_f = prepared

        # v4.3.0: skip-existing detection. If the output file already
        # exists with a matching video+audio codec, skip the encode.
        # v4.4.0: moved ABOVE the disk-space check so skipped files
        # don't trigger disk-space warnings. Combined into a single
        # log line with the [N/total] prefix.
        if self.skip_existing and self._output_already_encoded(file_path, output_f):
            self.skipped_count += 1
            vcodec = self.video_codec.ffprobe_codec_name or "?"
            acodec = self.audio_profile.ffprobe_codec_name or "?"
            self.log_msg.emit(
                f"[{idx}/{total}] {file_path.name} — SKIP (already {vcodec}/{acodec})"
            )
            self._check_consecutive_failures(file_path, accepted=True)
            self._cleanup_current_temps()
            if self.delete_source:
                self._sources_to_delete.append(file_path)
            return

        # v4.4.0: disk space pre-check for massive files. Warns (does NOT
        # abort) if free space on the output/temp partition is less than
        # the source size. Skipped for files < 1 GB. Runs ONLY for files
        # we're actually about to encode (after the skip-existing check).
        self._check_disk_space(file_path, output_f, needs_scale)

        # v4.4.0: emit the per-file banner HERE (not at the top) so skipped
        # files don't get a dangling "[N/total] filename" line. The final
        # OK/FAIL status line at the end repeats the prefix.
        self.log_msg.emit(f"[{idx}/{total}] {file_path.name}")

        # --- Encode ---
        encode_ok = self._encode_one(file_path, encode_input, output_f, worker_count)
        if not encode_ok:
            # ffmpeg fallback path: _ffmpeg_fallback_encode does NOT touch
            # _current_temps or fail_count, so we do both here to match the
            # original `else: self.fail_count += 1; self._cleanup_current_temps()`.
            # av1an path: _encode_one's `finally` already cleaned temps and
            # fail_count was incremented inside _encode_one.
            #
            # STOP exception: when the user clicked STOP mid-encode,
            # _run_with_stop_check returned "stop" and _ffmpeg_fallback_encode
            # returned False WITHOUT incrementing fail_count (a user abort is
            # not a transcode failure).  Honor that here by skipping the
            # fail_count increment when self._stop is set — temp cleanup
            # still runs so we don't leak intermediate files.
            if self.use_ffmpeg_fallback:
                if not self._stop:
                    self.fail_count += 1
                self._cleanup_current_temps()
            # v5-02: encode failure counts for consecutive-failure tracking
            # (but only if not a user STOP — a STOP is not a failure).
            if not self._stop:
                self._check_consecutive_failures(file_path, accepted=False)
            return

        # --- Post-encode verification + finalize ---
        accepted = self._verify_and_finalize(file_path, output_f, encode_input, needs_scale)
        if self.use_ffmpeg_fallback:
            # ffmpeg path always cleans up explicitly at every exit
            # (av1an path already cleaned up via _encode_one's `finally`).
            self._cleanup_current_temps()
        # accepted=True  -> success_count already incremented in _verify_and_finalize.
        # accepted=False -> fail_count already incremented + output unlinked there.

        # v5-02: check for consecutive failures with the same pattern.
        self._check_consecutive_failures(file_path, accepted)

    def _check_consecutive_failures(self, file_path: Path, accepted: bool):
        """v5-02: Track consecutive failures and auto-abort after 3.

        After 3 consecutive failures (regardless of pattern — if 3 files
        in a row fail, something is systematically wrong), auto-abort the
        queue with a clear message. The user can still click STOP to
        abort earlier.

        This prevents the scenario from the user's log: 46 files, all
        failing identically, processed one by one over ~2 hours. With
        this fix, the queue aborts after file 3.
        """
        if accepted:
            self._consecutive_fail_count = 0
            return

        self._consecutive_fail_count += 1
        if self._consecutive_fail_count >= 3 and not self._stop:
            self.log_msg.emit("")
            self.log_msg.emit(
                f"ABORT: {self._consecutive_fail_count} consecutive failures. "
                f"Auto-aborting queue — something is systematically wrong."
            )
            self.log_msg.emit(
                "  The remaining files will likely fail the same way. "
                "Fix the root cause (check the diagnostics above) and retry."
            )
            self.log_msg.emit(
                "  Common root causes: (1) all files are invalid (failed downloads), "
                "(2) av1an/encoder binary is broken, (3) out of disk space, "
                "(4) network mount is down."
            )
            self._stop = True

    def _validate_file(self, file_path):
        """ffprobe pre-validation. Returns (skip, info, src_w, src_h).

        skip=True signals the caller to abandon this file — the SKIP log
        line and fail_count increment have already happened here.

        If ffprobe cannot read the file, SKIP it. The encode fails ~99%
        of the time when ffprobe fails (failed download, HTML saved as
        .mp4, truncated, etc.). The `force=True` constructor flag
        overrides this for the rare edge case (rare codec, broken
        container metadata where ffprobe fails but ffmpeg can decode).
        """
        # Reuse pre-scanned dimensions if available, otherwise probe now
        prescan = self._file_res_map.get(file_path)
        src_w, src_h = (prescan[0], prescan[1]) if prescan else (None, None)
        info = None
        if self.env.ffprobe_path:
            info = ffprobe_validate(file_path, self.env.ffprobe_path)
        if info is None:
            if self.force:
                self.log_msg.emit(
                    f"WARN: ffprobe could not read {file_path.name} — "
                    f"attempting encode anyway (force=True)."
                )
            else:
                # Identify the file's actual type via `file` command.
                file_type = _identify_file_type(file_path)
                self.log_msg.emit(f"{self._status_prefix()}SKIP: not a valid video (ffprobe could not read it)")
                if file_type and self.verbose:
                    self._vlog(f"  File type: {file_type}")
                    if "HTML" in file_type or "ASCII" in file_type or "text" in file_type:
                        self._vlog(
                            "  This looks like a text/HTML file, not a video. "
                            "Common cause: failed yt-dlp download (region-locked, "
                            "age-restricted, or removed video). Re-download the file."
                        )
                    elif "data" in file_type:
                        self._vlog(
                            "  File type is 'data' — possibly truncated, encrypted, "
                            "or a partial download. Verify the file plays in mpv/VLC."
                        )
                if self.verbose:
                    self._vlog(
                        "  (Use the Force checkbox to attempt encode anyway.)"
                    )
                self.fail_count += 1
                return (True, None, None, None)
        else:
            duration = float(info.get("format", {}).get("duration", 0))
            has_video = any(s.get("codec_type") == "video" for s in info.get("streams", []))
            if not has_video:
                self.log_msg.emit(f"{self._status_prefix()}SKIP: no video stream")
                self.fail_count += 1
                return (True, None, None, None)
            if duration < 0.5:
                self.log_msg.emit(f"{self._status_prefix()}SKIP: too short ({duration:.1f}s)")
                self.fail_count += 1
                return (True, None, None, None)
            # Extract dims if pre-scan didn't have them
            if not src_w or not src_h:
                for s in info.get("streams", []):
                    if s.get("codec_type") == "video":
                        src_w = int(s.get("width", 0) or 0)
                        src_h = int(s.get("height", 0) or 0)
                        break
        return (False, info, src_w, src_h)

    def _prepare_input(self, file_path, src_w, src_h, needs_scale, scale_filter):
        """Pre-scale (if needed) and ensure the av1an work dir lands in temp.

        Returns (encode_input, output_f) on success, or None on failure
        (after logging + cleaning up current temps + incrementing fail_count).
        """
        # --- Pre-scale with ffmpeg if target resolution selected ---
        # ALL intermediates (scaled files, av1an work dirs) go to the app
        # temp directory so the user's video folders stay clean.
        encode_input = file_path

        # v4.6.0: pre-scale ONLY on the av1an path. The pure-ffmpeg path
        # (the default, and the only path NVENC can run on) scales inline
        # via the -vf args in _ffmpeg_fallback_encode — the intermediate
        # existed solely because VapourSynth source plugins choke on some
        # inputs ffmpeg handles fine. Skipping it on the ffmpeg path
        # removes the entire class of large-file failures: no 0.5-0.8x
        # source-size temp file, no extra full encode pass, and no
        # pre-scale timeout on long/high-bitrate sources.
        if needs_scale and not self.inline_scale and not self.use_ffmpeg_fallback:
            try:
                temp_scaled = _temp_path_for(file_path, ".scaled_tmp.mkv", worker_dir=self._temp_dir)
                self._current_temps.append(temp_scaled)
                # Use libx265 CRF 16 (visually lossless) for the intermediate —
                # NOT ffv1 (unsupported by VapourSynth source plugins: bestsource,
                # ffms2, lsmash — produces empty pipe → "Fatal: Failed to open
                # input file") and NOT CRF 0 (mathematically lossless → 2-4×
                # source size; a 20GB BluRay rip produced a 60-80GB intermediate
                # and crashed the encode with disk-exhaustion errors that
                # presented as cryptic "ffmpeg error (rc=234)" messages).
                # CRF 16 is transparent for archival purposes (~0.5-0.8× source
                # size) and HEVC-in-MKV is universally supported by every VS plugin.
                # For an even faster path that skips the intermediate entirely,
                # see the inline_scale option (passes scale filter to av1an via
                # --ffmpeg-filter-args).
                scale_cmd = [
                    self.env.ffmpeg_path,
                    "-i", str(file_path),
                    "-vf", scale_filter,
                    "-c:v", "libx265",
                    "-crf", "16",            # visually lossless — was 0 (mathematically lossless)
                    "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p",   # force 8-bit 4:2:0
                    "-y",
                    str(temp_scaled),
                ]
                # v4.2.1: Scaling notice is verbose-only.
                self._vlog(f"  Scaling {src_w or '?'}x{src_h or '?'} -> {self.resolution.width}x{self.resolution.height}...")
                # v4.6.0: run via _run_with_stop_check with the full
                # per-file timeout. The old flat
                # subprocess.run(timeout=1800) killed pre-scaling of
                # long/high-bitrate sources at exactly 30 minutes
                # ("FAIL: pre-scale error: Command ... timed out") — a
                # guaranteed large-file failure that also ignored the
                # STOP button for the whole intermediate pass.
                scale_status, scale_rc, _s_out, scale_err = self._run_with_stop_check(
                    scale_cmd, timeout=self.encode_timeout, log_prefix="  ",
                )
                if scale_status == "stop":
                    # User aborted — clean up the partial intermediate and
                    # bail WITHOUT counting a failure (a STOP is not an
                    # encode failure; the queue loop breaks next iteration).
                    self._cleanup_current_temps()
                    return None
                if scale_status == "timeout":
                    self.log_msg.emit(
                        f"{self._status_prefix()}FAIL: pre-scale timeout "
                        f"(exceeded {self.encode_timeout}s limit)"
                    )
                    temp_scaled.unlink(missing_ok=True)
                    self._cleanup_current_temps()
                    self.fail_count += 1
                    return None
                if scale_status == "ok" and scale_rc == 0 and temp_scaled.exists():
                    encode_input = temp_scaled
                    scaled_size = temp_scaled.stat().st_size / 1_048_576
                    # v4.2.1: verbose-only
                    self._vlog(f"  Pre-scale OK ({scaled_size:.1f} MB intermediate)")
                else:
                    stderr_snip = (scale_err or "")[-200:]
                    # v4.2.1: keep user-facing FAIL but shorten; stderr verbose-only
                    self.log_msg.emit(
                        f"{self._status_prefix()}FAIL: pre-scale failed (rc={scale_rc})"
                    )
                    if stderr_snip.strip():
                        self._vlog(f"  ffmpeg stderr: {stderr_snip.strip()}")
                    temp_scaled.unlink(missing_ok=True)
                    self._cleanup_current_temps()
                    self.fail_count += 1
                    return None
            except (OSError, subprocess.SubprocessError) as e:
                # v4.2.1: keep user-facing but shorten
                self.log_msg.emit(f"{self._status_prefix()}FAIL: pre-scale error: {e}")
                self._cleanup_current_temps()
                self.fail_count += 1
                return None

        # --- Ensure av1an work dir lands in the temp directory ---
        # av1an creates its work dir as {input_path}.av1an by default.
        # We do NOT use av1an's --temp flag because it causes "Error: End of file"
        # during scene detection when the input file is in the same directory
        # as --temp (av1an 0.5.2-unstable).  Instead, we ensure the -i argument
        # always points into the temp dir (pre-scaled files already live there;
        # for no-scale we create a symlink).
        if not encode_input.is_relative_to(self._temp_dir):
            symlink_path = _temp_path_for(file_path, encode_input.suffix, worker_dir=self._temp_dir)
            try:
                symlink_path.unlink(missing_ok=True)
                symlink_path.symlink_to(file_path.resolve())
                self._current_temps.append(symlink_path)
                encode_input = symlink_path
            except OSError as e:
                self.log_msg.emit(
                    f"  WARN: Could not create symlink in temp dir: {e}. "
                    f"av1an work dir will be created next to source file."
                )
        # Track the work dir where av1an will actually create it
        av1an_work = Path(f"{encode_input}.av1an")
        self._current_temps.append(av1an_work)

        # --- Build output path (preserve directory structure) ---
        rel_path = file_path.relative_to(self.in_dir)
        target_dir = self.out_dir / rel_path.parent
        target_dir.mkdir(parents=True, exist_ok=True)

        ext = self.container.ext
        # Always add resolution suffix when a target resolution is selected
        res_suffix = f"_{self.resolution.width}x{self.resolution.height}" if needs_scale else ""
        output_f = target_dir / f"{file_path.stem}{res_suffix}_archived.{ext}"

        return (encode_input, output_f)

    def _check_disk_space(self, file_path: Path, output_f: Path, needs_scale: bool) -> None:
        """v4.4.0: Warn if free disk space is less than the encode will need.

        v4.6.0: SEVERE warnings (free space below the source size on the
        partition we're about to write a big intermediate/output to) are
        now USER-FACING — they were verbose-only, so in the default quiet
        mode a batch that was going to die with "No space left on device"
        partway through gave zero advance notice. That silent failure was
        one of the "large files just fail" reports: small files fit in
        the remaining space, big ones didn't. Marginal advice (the 2-3x
        intermediate estimate) stays verbose-only.
        """
        try:
            src_size = file_path.stat().st_size
        except OSError:
            return  # can't stat source — skip the check
        if src_size < 1_073_741_824:  # < 1 GB — skip check for small files
            return
        src_gb = src_size / 1_073_741_824
        # Check output partition — severe when free < source size
        # (the encoded output is usually smaller, but the ffmpeg/av1an
        # buffer cache plus a same-partition temp can eat the difference).
        try:
            out_usage = shutil.disk_usage(output_f.parent)
            out_free_gb = out_usage.free / 1_073_741_824
            if out_free_gb < src_gb:
                self.log_msg.emit(
                    f"  WARN: low disk space on output ({out_free_gb:.1f} GB free, "
                    f"source is {src_gb:.1f} GB) — encode may fail partway through"
                )
            elif self.verbose and out_free_gb < src_gb * 2:
                self._vlog(
                    f"  WARN: output space getting tight ({out_free_gb:.1f} GB free, "
                    f"source is {src_gb:.1f} GB)"
                )
        except OSError:
            pass  # can't check — skip
        # When scaling on the av1an path, also check the temp partition
        # (the CRF-16 intermediate can be ~1x source size). The ffmpeg
        # path scales inline (no intermediate), so no temp warning there.
        if needs_scale and not self.use_ffmpeg_fallback:
            try:
                tmp_usage = shutil.disk_usage(self._temp_dir)
                tmp_free_gb = tmp_usage.free / 1_073_741_824
                # Severe: free temp space below the source size means the
                # intermediate will likely not fit → user-facing warning.
                if tmp_free_gb < src_gb:
                    self.log_msg.emit(
                        f"  WARN: low disk space on temp ({tmp_free_gb:.1f} GB free, "
                        f"source is {src_gb:.1f} GB) — the scale intermediate "
                        f"may not fit. Free space or enable 'Inline scale'."
                    )
                elif self.verbose and tmp_free_gb < src_gb * 2:
                    self._vlog(
                        f"  WARN: temp space getting tight ({tmp_free_gb:.1f} GB free, "
                        f"lossless intermediate may need ~{src_gb * 2:.1f} GB) — "
                        f"consider scaling to a smaller resolution or freeing space"
                    )
            except OSError:
                pass

    def _output_already_encoded(self, file_path: Path, output_f: Path) -> bool:
        """v4.3.0: Check if output_f already exists with a matching codec.

        Returns True (skip the encode) when ALL of the following hold:
          - output_f exists on disk
          - ffprobe can read it (not corrupt)
          - video stream codec_name matches self.video_codec.ffprobe_codec_name
          - audio stream codec_name matches self.audio_profile.ffprobe_codec_name
            (when both the profile and the file have an audio stream)
          - if scaling was requested, output resolution matches the target
        """
        if not output_f.exists():
            return False
        if not self.env.ffprobe_path:
            return False
        info = ffprobe_validate(output_f, self.env.ffprobe_path)
        if info is None:
            return False
        streams = info.get("streams", [])
        vstream = next((s for s in streams if s.get("codec_type") == "video"), None)
        astream = next((s for s in streams if s.get("codec_type") == "audio"), None)
        if not vstream:
            return False
        expected_v = self.video_codec.ffprobe_codec_name
        if expected_v and vstream.get("codec_name") != expected_v:
            return False
        expected_a = self.audio_profile.ffprobe_codec_name
        if expected_a and astream:
            if astream.get("codec_name") != expected_a:
                return False
        if self.resolution.width is not None and self.resolution.height is not None:
            actual_w = int(vstream.get("width", 0) or 0)
            actual_h = int(vstream.get("height", 0) or 0)
            if actual_w != self.resolution.width or actual_h != self.resolution.height:
                return False
        return True

    def _can_ffmpeg_fallback(self) -> bool:
        """v6-01: Check if ffmpeg has the encoder for this codec.

        Returns True if ffmpeg can encode with this codec's ffmpeg_encoder
        (e.g. libsvtav1, libvpx-vp9, libx265), False otherwise.
        Used to decide whether to retry a failed av1an encode with ffmpeg.
        """
        ffmpeg_enc = self.video_codec.ffmpeg_encoder
        lib_key = ffmpeg_lib_key_for(ffmpeg_enc)
        return bool(self.env.ffmpeg_libs.get(lib_key, False))

    def _encode_one(self, file_path, encode_input, output_f, worker_count,
                    chunk_method=None):
        """Dispatch to ffmpeg fallback or av1an. Returns True if encode succeeded.

        ffmpeg fallback: delegates to _ffmpeg_fallback_encode (which itself
        performs the size >=5% integrity check and unlinks bad output).  No
        temp cleanup or fail_count increment happens here for this path —
        _process_one_file handles both at the call site, matching the original.

        av1an: builds and runs the av1an command, performs the size >=5% check
        inline, and wraps everything in try/except/finally so temps are always
        cleaned up via _cleanup_current_temps() — matching the original.  On
        every failure path here, fail_count is incremented inside this method.

        v4.0.0: *chunk_method* is an explicit override used by the y4m-pipe-break
        retry path. When None, the method falls back to
        ``env.av1an_flags["chunk_method_override"]`` (set by env_probe or by
        a previous retry) or av1an's auto-selection. When av1an fails with the
        "Failed to read y4m frame delimiter" pattern (Hybrid chunk method on
        phone-recorded MP4s with sparse keyframes), this method recursively
        retries with ``chunk_method="select"`` and caches that choice so
        subsequent files skip the wasted first attempt.
        """
        # ── Choose encode path: av1an or ffmpeg fallback ──
        if self.use_ffmpeg_fallback:
            # ── Pure ffmpeg encode path ──
            self.log_msg.emit(f"  Mode: ffmpeg ({self.video_codec.ffmpeg_encoder})")
            return self._ffmpeg_fallback_encode(
                file_path, encode_input, output_f,
            )

        # ── av1an encode path (original) ──
        # Resolve encoder name with probe data
        enc = self.video_codec.av1an_encoder
        if enc in ("svt_av1", "svt") and "svt_name" in self.env.av1an_flags:
            enc = self.env.av1an_flags["svt_name"]

        # Build params via config table (no if/else).
        # v4.1.2: do NOT inject --threads into av1an's --video-params.
        # SvtAv1EncApp (the standalone CLI av1an invokes per-chunk) does
        # not accept --threads — only --lp (logical processors). Injecting
        # --threads produced "Unprocessed tokens: --threads" → every
        # chunk failed 3x → no av1an output. Thread capping is done via
        # av1an's --workers flag (chunk-parallel count) and via -threads
        # in the ffmpeg fallback path (where libsvtav1 is a library).
        v_params = self.video_codec.params_fn(self.crf, self.preset_val)

        # Audio params: dual-pass normalization per file, or simple volume
        audio_parts = list(self.audio_profile.params)
        if abs(self.audio_level_db) > 0.01:
            per_file_gain = self._analyze_audio_loudness(file_path)
            if per_file_gain is not None and abs(per_file_gain) > 0.01:
                audio_parts.extend(["-af", f"volume={per_file_gain:+.1f}dB"])
            else:
                # Fallback to knob's static value if analysis failed
                static_db = f"{self.audio_level_db:+.1f}".replace("+", "")
                audio_parts.extend(["-af", f"volume={static_db}dB"])
                self.log_msg.emit(f"  Audio: static gain {self.audio_level_db:+.1f} dB (analysis unavailable)")

        audio_str = " ".join(audio_parts)

        cmd = [
            self.env.av1an_path,
            "-i", str(encode_input),
            self.env.av1an_flags.get("worker", "--workers"), str(worker_count),
        ]

        # Chunk method: explicit arg (retry) > env override > av1an auto.
        # v4.0.0: when av1an auto-selects Hybrid (default when no VS source
        # plugins are installed), phone-recorded MP4s with sparse keyframes
        # fail with "Failed to read y4m frame delimiter". The retry path
        # passes chunk_method="select" which uses VapourSynth's select()
        # filter — slower but reliable.
        effective_chunk_method = (
            chunk_method
            or self.env.av1an_flags.get("chunk_method_override")
        )
        if effective_chunk_method:
            cmd.extend(["--chunk-method", effective_chunk_method])
        self.log_msg.emit(
            f"  Chunking: {effective_chunk_method or 'auto'} "
            f"(av1an default if no override)"
        )

        # v4.4.4: inline scale — when enabled and a target resolution is
        # selected, pass the scale/pad filter chain directly to av1an via
        # --ffmpeg-filter-args. This skips the CRF-16 intermediate encode
        # entirely (zero temp disk usage for the scaling step) at the cost
        # of running the filter on every chunk-extraction pass. The
        # default (inline_scale=False) uses the pre-scale intermediate,
        # which is more robust across av1an/VapourSynth versions but
        # requires the extra encode pass and 0.5-0.8× source size of temp
        # disk space for the intermediate.
        if self.inline_scale and self._current_scale_filter:
            cmd.extend(["--ffmpeg-filter-args", self._current_scale_filter])
            self.log_msg.emit(
                f"  Inline scale: enabled (filter passed via "
                f"--ffmpeg-filter-args, no intermediate file)"
            )

        cmd.extend([
            "--encoder", enc,
            self.env.av1an_flags.get("video_params", "--video-params"), v_params,
            self.env.av1an_flags.get("audio_params", "--audio-params"), audio_str,
            "--concat", self.env.av1an_flags.get("concat_method", "ffmpeg"),
            "-o", str(output_f),
        ])

        # Log the full av1an command for debugging
        self.log_msg.emit(f"  CMD: {' '.join(cmd)}")

        try:
            result = self._run_with_stop_check(
                cmd, env=_av1an_env(), timeout=self.encode_timeout, log_prefix="  ",
            )
            status, rc, stdout, stderr = result

            if status == "stop":
                # User requested STOP — do NOT increment fail_count (the
                # user explicitly chose to abort, it isn't a transcode
                # failure).  Remove partial output.  self._stop is already
                # True (set by the UI thread), so the orchestrator's
                # queue loop will break on the next iteration and emit
                # "STOP: Aborted by user."
                output_f.unlink(missing_ok=True)
                return False
            if status == "timeout":
                self.fail_count += 1
                self.log_msg.emit(f"{self._status_prefix()}FAIL: timeout (exceeded {self.encode_timeout}s limit)")
                return False

            # status == "ok" — wrap in CompletedProcess so the downstream
            # returncode check, diagnostic dump, and pattern matching are
            # byte-for-byte unchanged.
            res = subprocess.CompletedProcess(cmd, rc, stdout, stderr)

            if res.returncode == 0 and output_f.exists():
                src_size = file_path.stat().st_size
                out_size = output_f.stat().st_size
                ratio = out_size / src_size if src_size > 0 else 0

                # Integrity gate: 1KB absolute minimum. A valid container
                # header alone is ~1KB; anything below is definitely corrupt.
                # The duration check in _verify_and_finalize (≥95% of source
                # duration) is the real quality gate for high-bitrate sources.
                if out_size > 1024:
                    # Success — resolution/duration/subtitle/finalize happen
                    # in _verify_and_finalize (called by _process_one_file).
                    return True
                else:
                    self.fail_count += 1
                    self.log_msg.emit(
                        f"{self._status_prefix()}FAIL: output too small ({out_size / 1024:.0f} KB)"
                    )
                    # Remove corrupt output
                    output_f.unlink(missing_ok=True)
                    return False
            else:
                stderr_full = res.stderr or ""
                # v4.6.0: scan stdout too. av1an routes chunk-retry
                # noise (encoder stderr dumps, FRAME MISMATCH lines)
                # to stdout, so pattern-matching on stderr alone missed
                # the biggest real-world failure mode (see the FRAME
                # MISMATCH pattern below).
                combined_out = stderr_full + "\n" + (res.stdout or "")
                # v6: Don't increment fail_count yet — we may retry with
                # ffmpeg fallback below. Only increment if the retry also
                # fails (or no retry is possible).
                # v4.4.2: move the FAIL line to _vlog. If the ffmpeg
                # fallback succeeds, the user sees OK. If it also fails,
                # the RETRY FAIL path emits a user-facing FAIL. This way
                # the user doesn't see a confusing "FAIL then OK" for
                # files that av1an choked on but ffmpeg handled.
                self._vlog(
                    f"{self._status_prefix()}av1an failed (exit code {res.returncode}) — attempting ffmpeg fallback"
                )
                self._vlog("  ─── av1an stderr (last 25 lines) ───")
                stderr_lines = stderr_full.splitlines()
                for line in stderr_lines[-25:]:
                    self._vlog(f"  {line}")
                self._vlog("  ────────────────────────────────────")

                # Detect known av1an crash patterns and provide actionable fixes.
                # Pattern table — add new patterns here, no nested ifs below.
                # SEI CERT MSC04-C spirit: single source of truth for diagnostics.
                #
                # v5-03: Added "missing field `streams`" pattern — this is
                # the error av1an emits when its internal ffprobe call
                # returns JSON without a streams field, i.e. the input file
                # is not a valid video. Also added `file` command output
                # to the diagnostic so the user immediately sees "HTML
                # document" (failed yt-dlp download) instead of guessing.
                error_patterns: tuple[tuple[str, str, tuple[str, ...], bool], ...] = (
                    (
                        "Failed to get VSScript API",
                        "av1an cannot initialize VapourSynth — the binary was "
                        "compiled against a different VapourSynth version than "
                        "what is currently installed.",
                        (
                            "  FIX (Arch): yay -S av1an  OR  cargo install av1an --force --locked",
                            "  FIX (Debian): sudo apt install vapoursynth libvapoursynth-script-dev av1an",
                            "  FIX (other): rebuild av1an against current VapourSynth",
                            "  VapourSynth R77+ changed the VSScript API; av1an must be recompiled.",
                        ),
                        True,  # stop queue — every file will hit the same crash
                    ),
                    (
                        "No usable encoder found",
                        "av1an cannot find the encoder binary (SvtAv1EncApp / vpxenc / x265).",
                        (
                            "  Verify the encoder is installed and in PATH.",
                            "  Arch: pacman -S svt-av1 libvpx-tools x265",
                            "  Debian: apt install svt-av1 libvpx-tools x265",
                        ),
                        True,
                    ),
                    # v6-02: av1an scene-detection panic — per-file, not systematic.
                    (
                        "split scores is not empty",
                        "av1an panicked during scene detection (known av1an bug). "
                        "This is a per-file issue — the video content triggered a "
                        "Rust panic in av1an's split module. Will retry with ffmpeg.",
                        (
                            "  This is an av1an internal bug, not a file corruption issue.",
                            "  The file is a valid video — ffmpeg can encode it directly.",
                        ),
                        False,  # don't stop queue — retry with ffmpeg fallback
                    ),
                    (
                        "missing field `streams`",
                        "av1an's internal ffprobe call could not parse this file — "
                        "the file is not a valid video container. This is NOT an "
                        "av1an or ffmpeg bug; the input file itself is invalid.",
                        (
                            "  The file is likely a failed yt-dlp download (HTML error",
                            "  page saved as .mp4), a truncated download, or not a video",
                            "  at all. Run `file <filename>` to confirm.",
                        ),
                        False,  # don't stop queue — other files may be valid
                    ),
                    (
                        "Invalid data found when processing input",
                        "ffmpeg cannot read this input file — the file is corrupt, "
                        "truncated, or not a valid video container.",
                        (
                            "  Run `file <filename>` to see what the file actually is.",
                            "  If it's 'HTML document' or 'ASCII text', it's a failed",
                            "  yt-dlp download — re-download the source video.",
                            "  If it's 'data', the file may be truncated or encrypted.",
                        ),
                        False,
                    ),
                    (
                        "Error: End of file",
                        "av1an hit EOF during scene detection — usually a VapourSynth "
                        "source plugin issue with the intermediate file.",
                        (
                            "  Try a different --chunk-method (override via env probe).",
                            "  If pre-scaling, ensure the intermediate is libx265 CRF 0 (not ffv1).",
                        ),
                        False,
                    ),
                    (
                        "could not open input",
                        "av1an cannot read this input file — possibly corrupt or "
                        "an unsupported codec for the VapourSynth source plugin.",
                        (
                            "  Try playing the file with ffplay to verify it's not corrupt.",
                            "  Run: ffmpeg -i <file> -f null -  to see the decode error.",
                        ),
                        False,
                    ),
                    # v4.0.0: y4m pipe break — Hybrid chunk method can't handle
                    # files with sparse keyframes. This is the "works up until
                    # near the end, never saves chunks into a full file" bug.
                    # The encoder prints a SUMMARY block (it ran briefly on
                    # partial data before the pipe broke), which previously
                    # triggered the v6-03 "concat failure" misdiagnosis. The
                    # retry path switches to --chunk-method select which
                    # extracts frames one-by-one via VapourSynth's select()
                    # filter, avoiding the keyframe-alignment issue.
                    (
                        "Failed to read y4m frame delimiter",
                        "av1an's chunk extractor produced a broken y4m pipe — "
                        "the source's keyframe layout doesn't align with scene "
                        "boundaries. This is the Hybrid chunk method's known "
                        "failure mode for phone-recorded MP4s with sparse "
                        "keyframes (only I-frames every 5-10s). The encoder "
                        "printed a SUMMARY block because it ran briefly on "
                        "partial data before the pipe broke — this is NOT a "
                        "concat failure.",
                        (
                            "  Will retry with --chunk-method select (VapourSynth",
                            "  select() filter), which extracts frames one-by-one",
                            "  and avoids the keyframe-alignment issue.",
                            "  This is per-file, not systematic — subsequent files",
                            "  use select automatically.",
                        ),
                        False,  # don't stop queue — retry with select chunk method
                    ),
                    # v4.6.0: ffmpeg ≥ 7 removed the -vsync option that
                    # av1an's segment/hybrid chunk extraction passes to
                    # ffmpeg. Every segment-based chunk dies immediately
                    # ("Unrecognized option 'vsync'." → empty y4m pipe →
                    # chunk fails 3x). Verified on ffmpeg 9.0.2 + av1an
                    # 0.5.2. select (or a VS source plugin method) is the
                    # only working chunking on these systems.
                    (
                        "Unrecognized option 'vsync'",
                        "av1an's segment/hybrid chunk extraction calls "
                        "`ffmpeg -vsync`, which ffmpeg 7+ removed. Every "
                        "segment-based chunk fails instantly on this system.",
                        (
                            "  FIX: install a VapourSynth source plugin so av1an",
                            "  stops using ffmpeg segmenting: bestsource/ffms2/",
                            "  lsmash (e.g. on Arch: vapoursynth-plugin-bs).",
                            "  Alternatively stay on the default ffmpeg-only path",
                            "  (it doesn't use av1an chunking at all).",
                        ),
                        False,  # per-file — the select override keeps other files working
                    ),
                    # v4.6.0: frame-count drift between av1an's chunk
                    # manifest and what the encoder actually produced.
                    # av1an retries the chunk 3x, then shuts the worker
                    # down with the baffling "encoder crashed: exit
                    # status: 0" (exit 0 because the encode itself
                    # succeeded — on partial data). This was the dominant
                    # large-file failure in the 2026-07-13 av1an log and
                    # previously fell through to "Unknown av1an failure".
                    (
                        "FRAME MISMATCH",
                        "av1an's chunk manifest expected a different frame count "
                        "than the encoder produced — chunk-extraction drift on "
                        "sources with sparse/irregular keyframes. The encode "
                        "itself exits 0 (it ran on partial data), which av1an "
                        "reports as 'encoder crashed: exit status: 0'.",
                        (
                            "  Will retry with --chunk-method select, which",
                            "  extracts exact frame ranges and cannot drift.",
                            "  This is per-file, not systematic — subsequent",
                            "  files use select automatically.",
                        ),
                        False,  # don't stop queue — retry with select chunk method
                    ),
                )
                diagnosis_emitted = False
                for marker, summary, fixes, stop_queue in error_patterns:
                    # v4.6.0: scan stdout + stderr (FRAME MISMATCH and
                    # encoder dumps land in stdout).
                    if marker.lower() in combined_out.lower():
                        self.log_msg.emit("")
                        self.log_msg.emit(f"DIAGNOSIS: {summary}")
                        for fix in fixes:
                            self.log_msg.emit(fix)
                        # v5-03: run `file` on the input to tell the user
                        # what the file actually is. This is especially
                        # useful for "missing field streams" and "Invalid
                        # data found" — the user immediately sees "HTML
                        # document" instead of guessing.
                        if marker in ("missing field `streams`",
                                      "Invalid data found when processing input",
                                      "could not open input"):
                            file_type = _identify_file_type(file_path)
                            if file_type:
                                self.log_msg.emit(f"  File type: {file_type}")
                                if "HTML" in file_type or "ASCII" in file_type or "text" in file_type:
                                    self.log_msg.emit(
                                        "  → This is a TEXT file, not a video. "
                                        "Failed yt-dlp download — re-download the source."
                                    )
                                elif "data" in file_type and "ISO Media" not in file_type:
                                    self.log_msg.emit(
                                        "  → File type is 'data' — truncated, encrypted, "
                                        "or partial download."
                                    )
                        if stop_queue:
                            self._stop = True
                            self.log_msg.emit(
                                f"STOP: Skipping remaining files (same {marker} issue)."
                            )
                        diagnosis_emitted = True
                        break

                if not diagnosis_emitted:
                    # No known pattern matched — show the user where to look.
                    # v6-03: detect "encoder SUMMARY in stderr + non-zero exit"
                    # — the encoder succeeded but av1an failed to produce output.
                    # This is the "chunks but never saves a file" pattern caused
                    # by av1an's concat step failing.
                    # v4.0.0: only treat as concat failure when y4m break is NOT
                    # present. The y4m break pattern (above) emits its own
                    # diagnosis and triggers a retry with --chunk-method select.
                    # The SUMMARY block appears in both cases (encoder ran
                    # briefly before failing), so we must check for the y4m
                    # marker to avoid misdiagnosing chunk-extraction failures
                    # as concat failures.
                    if ("SUMMARY" in stderr_full
                            and "Average Speed" in stderr_full
                            and "Failed to read y4m frame delimiter" not in combined_out
                            and "FRAME MISMATCH" not in combined_out):
                        self.log_msg.emit("")
                        self.log_msg.emit(
                            "DIAGNOSIS: SVT-AV1 encoder completed successfully (SUMMARY"
                            " block found in stderr), but av1an failed to produce the"
                            " output file. This is an av1an concat failure — the encoder"
                            " did its job but av1an's post-encode merge step crashed."
                        )
                        self.log_msg.emit(
                            "  This is a known av1an bug on short videos (1-2 scenes)"
                            " where concat of a single chunk fails. Will retry with"
                            " ffmpeg fallback."
                        )
                    else:
                        self.log_msg.emit("")
                        self.log_msg.emit(
                            "DIAGNOSIS: Unknown av1an failure. Inspect the full stderr above."
                        )
                        # v5-03: run `file` on the input as a fallback diagnostic.
                        file_type = _identify_file_type(file_path)
                        if file_type:
                            self.log_msg.emit(f"  File type: {file_type}")
                        self.log_msg.emit(
                            "  Common causes: (1) out of disk space in temp dir, "
                            "(2) AV1 concat failed silently — try installing mkvtoolnix, "
                            "(3) av1an version too old for --concat flag — check av1an --help, "
                            "(4) input file is not a valid video (run `file <filename>`)."
                        )

                # ── v4.0.0: y4m pipe break retry — switch to --chunk-method select ──
                # If av1an failed with the y4m break pattern AND we're not
                # already using select, retry with --chunk-method select. This
                # is faster than the ffmpeg fallback (chunk-parallel still
                # works) and produces identical-quality output (same encoder,
                # same params). Cache the working method so subsequent files
                # skip the wasted first attempt.
                #
                # v4.6.0: FRAME MISMATCH (chunk-extraction drift, see the
                # error-pattern table) joins the y4m break as a drift
                # symptom that select fixes — it was previously an
                # "Unknown av1an failure" that went straight to the slow
                # full-file ffmpeg fallback.
                #
                # NOTE: Do NOT clean up _current_temps before the retry —
                # encode_input (symlink or pre-scaled file) is in
                # _current_temps and the recursive _encode_one call needs it.
                # The finally block below will clean up everything after the
                # recursive call returns (its own finally clears the list
                # first; our finally then runs on an empty list — no-op).
                extraction_drift = (
                    "Failed to read y4m frame delimiter" in combined_out
                    or "FRAME MISMATCH" in combined_out
                    # ffmpeg >= 7 removed -vsync: segment/hybrid chunking
                    # dies instantly while select still works (it uses the
                    # ffmpeg frame server, not segmenting).
                    or "Unrecognized option 'vsync'" in combined_out
                )
                if (not self._stop and extraction_drift
                        and effective_chunk_method != "select"
                        and self.env.av1an_flags.get("has_chunk_method", True)):
                    # v4.2.1: RETRY messages are verbose-only — the user
                    # already saw "FAIL" and will see "SUCCESS" if the retry
                    # works. They don't need to know the retry is happening.
                    self._vlog("")
                    self._vlog(
                        f"  RETRY: Re-encoding {file_path.name} with "
                        f"--chunk-method select (slower but reliable for "
                        f"files with sparse keyframes)..."
                    )
                    output_f.unlink(missing_ok=True)
                    # Cache for subsequent files — avoids the wasted first attempt
                    self.env.av1an_flags["chunk_method_override"] = "select"
                    return self._encode_one(
                        file_path, encode_input, output_f, worker_count,
                        chunk_method="select",
                    )

                # ── v6-01: Per-file av1an→ffmpeg fallback ──
                # If av1an failed for this file AND it's NOT a systematic issue
                # (VSScript API, missing encoder — those set self._stop=True),
                # AND ffmpeg has the encoder for this codec, retry with ffmpeg.
                # This handles:
                #   - av1an concat failures (encoder succeeded but no output)
                #   - av1an scene-detection panics ("split scores is not empty")
                #   - Any other per-file av1an internal failure
                #
                # NOTE: Do NOT clean up _current_temps before the retry —
                # encode_input (symlink or pre-scaled file) is in _current_temps
                # and _ffmpeg_fallback_encode needs it. The finally block below
                # will clean up everything after the retry completes.
                if not self._stop and self._can_ffmpeg_fallback():
                    self.log_msg.emit("")
                    self.log_msg.emit(
                        f"  RETRY: Attempting ffmpeg fallback for {file_path.name} "
                        f"({self.video_codec.ffmpeg_encoder})..."
                    )
                    # Remove any partial output av1an may have left
                    output_f.unlink(missing_ok=True)
                    # Retry with ffmpeg — _ffmpeg_fallback_encode does NOT
                    # increment fail_count on failure (the caller does that).
                    # If it succeeds, we return True WITHOUT incrementing
                    # fail_count — the file was saved, just via a different path.
                    fb_ok = self._ffmpeg_fallback_encode(
                        file_path, encode_input, output_f,
                    )
                    if fb_ok:
                        self._vlog(
                            f"  RETRY OK: ffmpeg fallback succeeded for {file_path.name}"
                        )
                        return True
                    else:
                        self.fail_count += 1
                        # v4.4.2: only user-facing FAIL for av1an path.
                        self.log_msg.emit(
                            f"{self._status_prefix()}FAIL: av1an + ffmpeg both failed"
                        )
                        self._vlog(
                            f"  RETRY FAIL: ffmpeg fallback also failed for {file_path.name}"
                        )
                        return False
                else:
                    # No retry possible — this is a systematic issue (stop_queue
                    # was set) or ffmpeg lacks the encoder.
                    self.fail_count += 1
                    self.log_msg.emit(
                        f"{self._status_prefix()}FAIL: av1an (no ffmpeg fallback available)"
                    )
                    return False
        except (OSError, subprocess.SubprocessError) as e:
            self.fail_count += 1
            self.log_msg.emit(f"{self._status_prefix()}FAIL: system error: {e}")
            return False
        finally:
            # Always clean this file's temps before moving to next
            self._cleanup_current_temps()

    def _verify_and_finalize(self, file_path, output_f, encode_input, needs_scale):
        """Post-encode verification + subtitle mux + source deletion deferral.

        Runs after a successful _encode_one.  Performs:
          - output resolution verification (if scaling was requested)
          - duration integrity check (>= 95% of source)
          - subtitle mux (if requested)
          - success_count increment + SUCCESS log
          - source deletion deferral (if delete_source is set)

        Returns True if the file was accepted, False if any check failed.
        On failure, fail_count is incremented and output_f is unlinked before
        returning False.  Temp cleanup is the caller's responsibility — it
        differs between the av1an path (already done in _encode_one's finally)
        and the ffmpeg fallback path (done explicitly in _process_one_file).
        """
        # Post-encode resolution verification
        if needs_scale and self.env.ffprobe_path:
            if not _verify_output_resolution(
                output_f, self.env.ffprobe_path,
                self.resolution.width, self.resolution.height,
            ):
                self.fail_count += 1
                self.log_msg.emit(
                    f"{self._status_prefix()}FAIL: resolution verification failed "
                    f"(expected {self.resolution.width}x{self.resolution.height})"
                )
                output_f.unlink(missing_ok=True)
                return False
        src_size = file_path.stat().st_size
        out_size = output_f.stat().st_size
        ratio = out_size / src_size if src_size > 0 else 0

        # Duration integrity check (>= 95% of source)
        dur_ok = True
        dur_info = ""
        if self.env.ffprobe_path:
            src_dur = ffprobe_duration(file_path, self.env.ffprobe_path)
            out_dur = ffprobe_duration(output_f, self.env.ffprobe_path)
            if src_dur and out_dur:
                dur_ratio = out_dur / src_dur
                dur_ok = dur_ratio >= 0.95
                dur_info = f", duration {out_dur:.1f}s/{src_dur:.1f}s ({dur_ratio * 100:.0f}%)"

        if not dur_ok:
            self.fail_count += 1
            self.log_msg.emit(f"{self._status_prefix()}FAIL: duration mismatch{dur_info}")
            output_f.unlink(missing_ok=True)
            return False

        # Mux subtitle if requested (needs source file intact)
        if self.subtitle_lang:
            self._mux_subtitle(file_path, output_f)

        self.success_count += 1
        # v4.4.0: combined single-line status with [N/total] prefix.
        prefix = self._status_prefix()
        if self.verbose:
            self.log_msg.emit(
                f"{prefix}SUCCESS: {src_size / 1_048_576:.1f}MB -> {out_size / 1_048_576:.1f}MB "
                f"({ratio * 100:.0f}%{dur_info})"
            )
        else:
            self.log_msg.emit(
                f"{prefix}OK: {src_size / 1_048_576:.1f}MB -> {out_size / 1_048_576:.1f}MB "
                f"({ratio * 100:.0f}%)"
            )
        # Defer source deletion until after final cleanup
        if self.delete_source:
            self._sources_to_delete.append(file_path)
        return True

    def _cleanup_current_temps(self):
        """Remove all tracked temp files/dirs for the current file.

        Resilient: each removal is try/except'd individually so one bad path
        doesn't block the rest.  Clears the tracking list when done.
        """
        for tf in self._current_temps:
            try:
                if tf.is_dir():
                    shutil.rmtree(str(tf), ignore_errors=True)
                elif tf.exists():
                    tf.unlink()
            except Exception:
                pass
        self._current_temps.clear()

    def _final_cleanup_sweep(self):
        """Residual sweep to catch any orphaned temp files.

        v3 (OTC-013): primary target is now the per-worker subdir
        (``~/.cache/OpenTranscode/tmp/worker-<pid>/``), NOT the shared
        app temp dir. This is safe because the subdir ONLY contains
        this worker's intermediates — a concurrent worker has its own
        subdir. The previous "nuclear" sweep of the entire app temp
        dir was a race-condition risk that this eliminates.
        Also scans in_dir/out_dir as a safety net for legacy temp files
        written by older versions that placed temps next to source files.
        """
        swept = 0

        # v3: sweep ONLY this worker's per-PID subdir, not the shared parent.
        # This is safe — the subdir contains only this worker's intermediates.
        if self._temp_dir.is_dir():
            for hit in self._temp_dir.iterdir():
                try:
                    if hit.is_dir():
                        shutil.rmtree(str(hit), ignore_errors=True)
                    else:
                        hit.unlink(missing_ok=True)
                    swept += 1
                except OSError:
                    # SEI CERT ERR01-C: narrow to OSError (file ops).
                    # Best-effort sweep must not crash on a single bad path.
                    pass

        # Safety-net sweep of user directories (for legacy temp files
        # written by older versions that placed temps next to source files)
        legacy_patterns = ["*.scaled_tmp.mkv", "*.av1an", "*_encodes", "*.*.av1an"]
        for search_dir in (self.in_dir, self.out_dir):
            if not search_dir.is_dir():
                continue
            for pattern in legacy_patterns:
                for hit in search_dir.rglob(pattern):
                    try:
                        if hit.is_dir():
                            shutil.rmtree(str(hit), ignore_errors=True)
                        else:
                            hit.unlink(missing_ok=True)
                        swept += 1
                    except OSError:
                        pass
        # Also clean any orphans still in _current_temps (e.g. stop/crash mid-loop)
        self._cleanup_current_temps()
        # v3: remove the now-empty per-worker subdir itself.
        try:
            self._temp_dir.rmdir()
        except OSError:
            pass  # not empty / not ours — leave it
        if swept:
            self.log_msg.emit(f"CLEANUP: Swept {swept} residual temp file(s)/dir(s).")

    # ── Audio loudness analysis (dual-pass normalization) ──

    def _analyze_audio_loudness(self, file_path: Path) -> float | None:
        """Dual-pass loudnorm analysis for a single file.

        Pass 1: Run loudnorm in analysis-only mode to measure the file's current
        integrated loudness (I) and true peak (TP).

        Returns the dB gain to apply, or None if analysis fails (falls back to
        the knob's static value).
        """
        if not self.env.ffmpeg_path:
            return None
        if abs(self.audio_level_db) < 0.01:
            return None  # knob is at 0 — no normalization requested

        target_lufs = self.audio_level_db  # knob value IS the target LUFS

        try:
            # Pass 1: analyze current loudness
            # v4.6.0: -vn skips video decoding — without it the analysis
            # decoded the ENTIRE video stream just to measure audio
            # loudness, which pushed long/large files past the 120s
            # timeout and silently degraded every big file to the static
            # knob gain.
            analysis_cmd = [
                self.env.ffmpeg_path,
                "-i", str(file_path),
                "-vn",
                "-af", (
                    f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:"
                    f"print_format=json"
                ),
                "-f", "null", "-",
            ]
            res = subprocess.run(
                analysis_cmd, capture_output=True, text=True, timeout=120,
            )

            # Parse the JSON stats from stderr (loudnorm prints to stderr)
            stderr = res.stderr or ""

            # Find the JSON block
            json_match = re.search(r'\{[^{}]*"input_i"[^{}]*\}', stderr, re.DOTALL)
            if not json_match:
                return None

            stats = json.loads(json_match.group())

            input_i = float(stats.get("input_i", "-99"))
            input_tp = float(stats.get("input_tp", "-99"))
            target_tp = float(stats.get("target_tp", "-1.5"))

            # If file is already silent or near-silent, skip
            if input_i <= -70:
                return None

            # Compute the gain loudnorm would apply
            gain_db = target_lufs - input_i

            # Pass 2 concept: check if applying this gain would push peaks
            # above our ceiling. The ceiling is target_tp (default -1.5 dBTP).
            # We want 15% headroom below that ceiling.
            headroom_db = abs(target_tp) * 0.15
            peak_ceiling = target_tp + headroom_db

            # If the file's true peak + gain would exceed the ceiling, clamp
            projected_peak = input_tp + gain_db
            if projected_peak > peak_ceiling:
                gain_db = peak_ceiling - input_tp

            self.log_msg.emit(
                f"  Audio: {input_i:.1f} LUFS -> {target_lufs:.1f} LUFS "
                f"(gain {gain_db:+.1f} dB, peak {input_tp:.1f} -> "
                f"{input_tp + gain_db:.1f} dBTP)"
            )
            return gain_db

        except (OSError, subprocess.SubprocessError, ValueError) as e:
            # ValueError covers json.JSONDecodeError and float() parse failures
            self.log_msg.emit(f"  Audio: loudnorm analysis failed ({e}), using knob value")
            return None

    # ── Subtitle extraction & muxing ──

    def _find_subtitle_stream(self, source: Path, lang: str) -> tuple[int | None, str]:
        """Find subtitle stream in source matching language code.
        Prefers forced disposition tracks. Returns (stream_index, codec_name)."""
        if not self.env.ffprobe_path:
            return (None, "")

        info = ffprobe_validate(source, self.env.ffprobe_path)
        if not info:
            return (None, "")

        forced_match = None
        any_match = None

        for stream in info.get("streams", []):
            if stream.get("codec_type") != "subtitle":
                continue
            tags = stream.get("tags", {})
            if tags.get("language", "").lower() != lang.lower():
                continue

            idx = stream.get("index")
            codec = stream.get("codec_name", "")
            disposition = stream.get("disposition", {})

            if disposition.get("forced") and forced_match is None:
                forced_match = (idx, codec)
            if any_match is None:
                any_match = (idx, codec)

        return forced_match if forced_match else (any_match or (None, ""))

    def _mux_subtitle(self, source: Path, output: Path):
        """Mux a subtitle track from source into the encoded output (soft sub).
        Uses stream copy for MKV; converts to WebVTT for WebM containers."""
        sub_idx, sub_codec = self._find_subtitle_stream(source, self.subtitle_lang)

        if sub_idx is None:
            self.log_msg.emit(f"  SUBS: No {self.subtitle_lang} subtitle found in {source.name}")
            return

        # WebM only supports WebVTT natively; MKV carries any subtitle codec
        is_webm = output.suffix.lower() == ".webm"
        sub_codec_flag = "copy" if not is_webm else "webvtt"

        tmp_out = output.with_suffix(output.suffix + ".submux_tmp")
        try:
            cmd = [
                self.env.ffmpeg_path,
                "-i", str(output),       # encoded output (video + audio)
                "-i", str(source),       # original source (subtitle source)
                "-map", "0",              # all streams from encoded output
                "-map", "-0:s",           # strip any subtitle from output
                "-map", f"1:{sub_idx}",   # subtitle from source
                "-c:v", "copy",
                "-c:a", "copy",
                "-c:s", sub_codec_flag,
                "-y",
                str(tmp_out),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True)

            if res.returncode == 0 and tmp_out.exists() and tmp_out.stat().st_size > 0:
                output.unlink()
                tmp_out.rename(output)
                self.log_msg.emit(
                    f"  SUBS: Muxed {self.subtitle_lang} sub ({sub_codec}) into {output.name}"
                )
            else:
                tmp_out.unlink(missing_ok=True)
                tail = (res.stderr or "")[-200:]
                self.log_msg.emit(f"  SUBS WARN: Remux failed for {output.name}: {tail}")
        except (OSError, subprocess.SubprocessError) as e:
            tmp_out.unlink(missing_ok=True)
            self.log_msg.emit(f"  SUBS ERROR: {e}")

    def stop(self):
        self._stop = True


# ──────────────────────────────────────────────
#  SOURCE BUILD WORKER — compile VS + av1an from git
# ──────────────────────────────────────────────

# ──────────────────────────────────────────────
#  BUILD DEPENDENCY TREE (v4.7.1 — distro-aware)
# ──────────────────────────────────────────────

# Binaries the build needs. pkgconf/pkg-config and python/python3 are
# aliased — any one of each pair satisfies the check.
BUILD_TOOL_ALIASES: dict[str, tuple[str, ...]] = {
    "git": ("git",),
    "meson": ("meson",),
    "ninja": ("ninja",),
    "c++ compiler": ("g++", "c++", "clang++"),
    "make": ("make",),
    "pkg-config": ("pkg-config", "pkgconf"),
    "python3": ("python3",),
    "nasm": ("nasm",),
    "cmake": ("cmake",),
}

# Packages providing the toolchain + the libraries the builds link
# against (zimg is VapourSynth's one hard library dependency; rust is
# only needed for the av1an build).
BUILD_DEPS_BY_FAMILY: dict[str, list[str]] = {
    "arch":   ["base-devel", "meson", "ninja", "cmake", "nasm", "git",
               "python", "pkgconf", "zimg", "rust"],
    "debian": ["build-essential", "meson", "ninja-build", "cmake", "nasm",
               "git", "python3", "python3-dev", "pkg-config", "libzimg-dev",
               "cargo", "rustc"],
    "redhat": ["gcc", "gcc-c++", "make", "meson", "ninja-build", "cmake",
               "nasm", "git", "python3", "python3-devel",
               "pkgconf-pkg-config", "zimg-devel", "cargo", "rust"],
    "suse":   ["gcc", "gcc-c++", "make", "meson", "ninja", "cmake", "nasm",
               "git", "python3", "python3-devel", "pkg-config",
               "zimg-devel", "rust", "cargo"],
}

PKG_INSTALL_CMD: dict[str, list[str]] = {
    "arch":   ["pacman", "-S", "--needed", "--noconfirm"],
    "debian": ["apt-get", "install", "-y"],
    "redhat": ["dnf", "install", "-y"],
    "suse":   ["zypper", "--non-interactive", "install"],
}

MANUAL_DEP_NOTE = (
    "No automatic package install for this distro family. Install a C++ "
    "toolchain plus meson, ninja, cmake, nasm, git, python3, pkg-config, "
    "zimg development headers{rust} manually, then press REBUILD again."
)

GPU_BUILD_PACKAGES: dict[str, dict[str, list[str]]] = {
    "nvenc": {
        "arch":   ["nv-codec-headers"],
        "debian": [],
        "redhat": [],
        "suse":   [],
    },
    "vaapi": {
        "arch":   ["libva", "libdrm", "mesa"],
        "debian": ["libva-dev", "libdrm-dev", "mesa-va-drivers"],
        "redhat": ["libva-devel", "libdrm-devel", "mesa-va-drivers"],
        "suse":   ["libva-devel", "libdrm-devel", "Mesa-libva"],
    },
    "qsv": {
        "arch":   ["libva", "intel-media-driver", "onevpl"],
        "debian": ["libva-dev", "intel-media-va-driver-non-free", "libvpl-dev"],
        "redhat": ["libva-devel", "intel-media-driver", "oneVPL-devel"],
        "suse":   ["libva-devel", "intel-media-driver", "oneVPL-devel"],
    },
}







@dataclass
class DepPlan:
    """What the rebuild needs, and how to get it on this distro."""
    packages: list[str] = field(default_factory=list)
    install_cmd: list[str] | None = None
    manual_note: str | None = None


def build_dep_plan(distro: DistroProfile, build_av1an: bool = True) -> DepPlan:
    """Pure: the package list + install command for this distro family.

    Works even when the environment probe failed — it only needs the
    distro family, which is detectable from /etc/os-release alone.
    """
    packages = list(BUILD_DEPS_BY_FAMILY.get(distro.family, []))
    if not build_av1an:
        for rust_pkg in ("rust", "rustc", "cargo"):
            if rust_pkg in packages:
                packages.remove(rust_pkg)
    install_cmd = PKG_INSTALL_CMD.get(distro.family)
    manual_note = None
    if not install_cmd or not packages:
        manual_note = MANUAL_DEP_NOTE.format(
            rust=" and Rust/cargo" if build_av1an else "")
    return DepPlan(packages=packages, install_cmd=install_cmd,
                   manual_note=manual_note)

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
        key="nv-ampere",
        label="NVIDIA Ampere (RTX 30-series, A10/A40/A2, CMP 90HX) — H.264 + HEVC (no AV1 encode)",
        vendor="nvidia", api="nvenc",
        encoders=dict(_NV),
        match=("RTX 30", "3090", "3080", "3070", "3060", "3050",
               "A10 ", "A40", "A2 ", "CMP 90"),
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
        key="nv-compute",
        label="NVIDIA data-center compute (V100/A100/H100, CMP 170HX) — no NVENC (CPU path)",
        vendor="nvidia", api="none",
        encoders={},
        match=("V100", "A100", "H100", "B200", "GB200", "CMP 170"),
        notes="Compute boards ship without NVENC silicon. CMP 170HX is "
              "GA100-based — the fastest mining card that cannot hardware-encode.",
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

GPU_PROFILES_BY_KEY: dict[str, GpuProfile] = {p.key: p for p in GPU_PROFILES}


def gpu_profile_by_key(key: str | None) -> GpuProfile | None:
    if not key:
        return None
    return GPU_PROFILES_BY_KEY.get(key)


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


class SourceBuildWorker(QThread):
    """Builds VapourSynth and/or av1an from git to resolve ABI mismatches.

    Runs in a background thread.  Emits progress via log_msg.
    When done, emits build_done(success, message).

    Everything installs to the user's home directory (no sudo for install):
      VapourSynth → ~/.local/lib/  (av1an finds it via LD_LIBRARY_PATH)
      av1an       → ~/.cargo/bin/   (already in PATH)
    Only build-dependency installation (pacman -S) may need sudo.
    """
    log_msg    = Signal(str)
    build_done = Signal(bool, str)   # (success, detail)

    def __init__(self, build_vs: bool = True, build_av1an: bool = True,
                 build_ffmpeg_iamf: bool = False, gpu_profile_key: str = ""):
        super().__init__()
        self.build_vs = build_vs
        self.build_av1an = build_av1an
        self.build_ffmpeg_iamf = build_ffmpeg_iamf
        # v4.8.0: selected GPU capability profile — extends the dep tree
        # with the vendor's build/runtime packages.
        self.gpu_profile_key = gpu_profile_key
        self._stop = False
        # Private per-worker environment snapshot. Mutating os.environ is
        # process-global and leaks across threads/subsequent subprocesses;
        # _build_env is local to this worker and passed via env= to every
        # subprocess.run call below (see _run_cmd).
        self._build_env: dict[str, str] = os.environ.copy()

    def _extend_env(self, var: str, value: str, prepend: bool = False):
        """Add ``value`` to ``self._build_env[var]`` (NOT ``os.environ``).

        ``prepend=True`` places ``value`` first so it shadows any existing
        entry (e.g. ~/.local/bin must shadow /usr/bin, libiamf's
        PKG_CONFIG_PATH must shadow the system pkgconfig dir); default
        appends (e.g. extending PATH with ~/.cargo/bin). Caller is
        responsible for any idempotency check (matches the original
        per-site ``if x not in existing:`` pattern). rstrip(":") on
        prepend avoids a trailing colon when ``var`` was previously unset.
        """
        existing = self._build_env.get(var, "")
        if prepend:
            self._build_env[var] = f"{value}:{existing}".rstrip(":")
        else:
            self._build_env[var] = f"{existing}:{value}" if existing else value

    def _run_cmd(self, cmd, cwd=None, timeout=600, label=""):
        """Run a command, log output, return (returncode, combined_output)."""
        self.log_msg.emit(f"  $ {' '.join(cmd[:6])}{'...' if len(cmd)>6 else ''}")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout, cwd=cwd, env=self._build_env)
            # Log last few lines of stderr for diagnostics
            if r.stderr:
                for line in r.stderr.strip().splitlines()[-5:]:
                    self.log_msg.emit(f"    {line}")
            if r.returncode != 0 and r.stdout:
                for line in r.stdout.strip().splitlines()[-3:]:
                    self.log_msg.emit(f"    {line}")
            return r.returncode, (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            self.log_msg.emit(f"  TIMEOUT ({timeout}s) running: {label or cmd[0]}")
            return -1, f"timeout after {timeout}s"
        except (OSError, subprocess.SubprocessError) as e:
            self.log_msg.emit(f"  ERROR: {e}")
            return -1, str(e)

    def _sudo_cmd(self, cmd, timeout=120, label=""):
        """Run a command with sudo (or pkexec as graphical fallback)."""
        # Try pkexec first (graphical polkit prompt — works in desktop sessions)
        pkexec = shutil.which("pkexec")
        if pkexec:
            return self._run_cmd([pkexec] + cmd, timeout=timeout, label=label or cmd[0])
        # Fall back to sudo (needs a terminal; may fail silently)
        return self._run_cmd(["sudo"] + cmd, timeout=timeout, label=label or cmd[0])

    def run(self):
        try:
            # ── v4.7.1: distro-aware dependency tree ──
            # The rebuild must work on a bare system: detect missing
            # tools/libraries and install them via the distro package
            # manager (one privilege prompt via pkexec/sudo) BEFORE
            # compiling anything.
            self.log_msg.emit("")
            self.log_msg.emit("=== Generating dependency tree ===")
            self._distro = detect_distro()
            self.log_msg.emit(
                f"  Distro: {self._distro.name} (family={self._distro.family})"
            )
            plan = build_dep_plan(self._distro, build_av1an=self.build_av1an)

            # v4.8.0: GPU-profile packages on top of the base toolchain.
            gpu_profile = gpu_profile_by_key(self.gpu_profile_key)
            if gpu_profile is not None and gpu_profile.api != "none":
                gpu_pkgs = gpu_dep_packages(gpu_profile.api, self._distro.family)
                if gpu_pkgs:
                    self.log_msg.emit(
                        f"  GPU profile {gpu_profile.key} ({gpu_profile.api}): "
                        f"+{len(gpu_pkgs)} package(s)"
                    )
                    plan.packages.extend(p for p in gpu_pkgs
                                         if p not in plan.packages)

            missing = self._missing_build_tools()
            zimg_ok = self._pkgconfig_exists("zimg")
            if zimg_ok:
                self.log_msg.emit("  OK: zimg (VapourSynth dependency)")
            else:
                missing.append("zimg (library, via pkg-config)")
            if self.build_av1an and not shutil.which("cargo"):
                missing.append("cargo (rust)")

            if missing:
                self.log_msg.emit(f"  Missing: {', '.join(missing)}")
                if plan.install_cmd:
                    self.log_msg.emit(
                        f"  Installing {len(plan.packages)} package(s) via "
                        f"{plan.install_cmd[0]} (privilege prompt possible)..."
                    )
                    rc, _ = self._sudo_cmd(
                        plan.install_cmd + plan.packages,
                        timeout=900, label=f"{plan.install_cmd[0]} build-deps",
                    )
                    if rc != 0:
                        self.log_msg.emit(
                            "  (install reported an error — continuing; "
                            "some packages may already be present)"
                        )
                else:
                    self.log_msg.emit(f"  {plan.manual_note}")
            else:
                self.log_msg.emit("  All build dependencies already installed.")

            # Re-verify the critical tools after install.
            still_missing = self._missing_build_tools()
            if still_missing:
                self.log_msg.emit(
                    f"  FATAL: still missing after install: {', '.join(still_missing)}. "
                    f"Install them manually and press REBUILD again."
                )
                self.build_done.emit(False, f"missing build tools: {still_missing}")
                return

            # Ensure cargo is in PATH after potential install.
            # NOTE: /root/.cargo/bin was dropped (OTC-015/v3-08) — root's
            # cargo dir is not readable by a non-root user. ~/.cargo/bin
            # covers the user's rustup install; /usr/bin is already in the
            # default PATH and is appended here only to match the original
            # mutation's intent (cargo from pacman lives there).
            self._extend_env("PATH", "/usr/bin")
            self._extend_env("PATH", str(Path.home() / ".cargo" / "bin"))
            if not shutil.which("cargo") and self.build_av1an:
                self.log_msg.emit("  FATAL: cargo not found after deps install. Aborting.")
                self.build_done.emit(False, "Rust/cargo not available")
                return

            # ── Optional: ffmpeg build deps (libopus, libvorbis dev pkgs) ──
            if self.build_ffmpeg_iamf:
                self._install_ffmpeg_build_deps()

            # ── Build & install VapourSynth to ~/.local (NO sudo needed) ──
            if self.build_vs:
                self._build_vapoursynth()
                # v4.7.1: BestSource right after VS, compiled against the
                # fresh VS headers — gives av1an a fast chunk method.
                self._build_bestsource()

            # ── Build av1an to ~/.cargo/bin (NO sudo needed) ──
            if self.build_av1an:
                self._build_av1an()

            # ── Build libiamf + ffmpeg with --enable-libiamf to ~/.local ──
            if self.build_ffmpeg_iamf:
                self._build_libiamf()
                self._build_ffmpeg_with_iamf()

            # ── Ensure the runtime env can find the fresh VS stack ──
            # The git VapourSynth installs self-contained into the user
            # site-packages (module + libs + plugins). av1an dlopens
            # libvapoursynth-script from there, so both LD_LIBRARY_PATH
            # and PYTHONPATH must include it.
            local_lib = str(Path.home() / ".local" / "lib")
            existing_ld = self._build_env.get("LD_LIBRARY_PATH", "")
            if local_lib not in existing_ld:
                self._extend_env("LD_LIBRARY_PATH", local_lib, prepend=True)
            user_site = self._vs_user_site()
            if user_site and (user_site / "vapoursynth" / "libvsscript.so").exists():
                vs_dir = str(user_site / "vapoursynth")
                if vs_dir not in self._build_env.get("LD_LIBRARY_PATH", ""):
                    self._extend_env("LD_LIBRARY_PATH", vs_dir, prepend=True)
                if str(user_site) not in self._build_env.get("PYTHONPATH", ""):
                    self._extend_env("PYTHONPATH", str(user_site), prepend=True)
                self.log_msg.emit(
                    f"  Runtime env: LD_LIBRARY_PATH/PYTHONPATH include {vs_dir}"
                )

            self.log_msg.emit("")
            self.log_msg.emit("=== Source build complete ===")
            self.build_done.emit(True, "Build and install completed (local ~/.local/).")

        except Exception as e:
            # SEI CERT ERR01-C: justified — this method orchestrates a long
            # multi-step build (git clone, meson, ninja, cargo install) whose
            # helper methods signal failure by `raise Exception(msg)` (15
            # sites). Catching Exception here converts any of those into a
            # user-facing build_done(False, ...) signal instead of crashing
            # the QThread. Narrowing would require refactoring all `raise
            # Exception(...)` call sites — out of scope for ERR01-C pass.
            self.log_msg.emit(f"BUILD FAILED: {e}")
            self.build_done.emit(False, str(e))

    def _missing_build_tools(self) -> list[str]:
        """Binaries from BUILD_TOOL_ALIASES that are not on PATH."""
        missing = []
        for label, candidates in BUILD_TOOL_ALIASES.items():
            if not any(shutil.which(c) for c in candidates):
                missing.append(label)
        return missing

    def _pkgconfig_exists(self, name: str) -> bool:
        rc, _ = self._run_cmd(
            ["pkg-config", "--exists", name],
            timeout=10, label=f"pkg-config {name}",
        )
        return rc == 0

    def _vs_user_site(self) -> Path | None:
        """The user site-packages dir of the system python3 — where the
        VapourSynth git install places its self-contained stack (module,
        libs, headers, plugins/)."""
        rc, out = self._run_cmd(
            ["python3", "-m", "site", "--user-site"],
            timeout=15, label="python3 -m site --user-site",
        )
        if rc == 0 and out.strip():
            return Path(out.strip().splitlines()[-1])
        return None

    def _build_bestsource(self):
        """Clone and build the BestSource VapourSynth plugin from git.

        BestSource gives av1an a fast, frame-accurate chunk source — the
        difference between 'select' (quadratic decoding, minutes per
        file) and normal chunk-parallel speed. Compiled against the
        vapoursynth headers of the JUST-INSTALLED git VS (via
        PYTHONPATH/PKG_CONFIG_PATH), so the plugin ABI always matches
        the VS that av1an will load. Requires the repo's libp2p
        submodule (initialized here).
        """
        self.log_msg.emit("")
        self.log_msg.emit("=== Building BestSource plugin from git ===")
        self.log_msg.emit("  Source: https://github.com/vapoursynth/bestsource")

        build_dir = Path("/tmp/bestsource-git-build")
        if build_dir.exists():
            shutil.rmtree(build_dir, ignore_errors=True)

        rc, out = self._run_cmd(
            ["git", "clone", "--depth", "1",
             "https://github.com/vapoursynth/bestsource.git",
             str(build_dir)],
            timeout=120, label="git clone bestsource",
        )
        if rc != 0:
            raise Exception(f"git clone bestsource failed: {out[-300:]}")

        # libp2p is a required submodule (R9+ builds source from it).
        rc, out = self._run_cmd(
            ["git", "submodule", "update", "--init", "--depth", "1"],
            cwd=str(build_dir), timeout=120, label="git submodule update",
        )
        if rc != 0:
            raise Exception(f"bestsource submodule init failed: {out[-300:]}")

        # Point meson/pkg-config at the freshly built VS stack.
        user_site = self._vs_user_site()
        if user_site and (user_site / "vapoursynth").is_dir():
            self._extend_env("PYTHONPATH", str(user_site), prepend=True)
            self._extend_env("PKG_CONFIG_PATH",
                             str(user_site / "vapoursynth" / "pkgconfig"),
                             prepend=True)
            self._extend_env("LD_LIBRARY_PATH",
                             str(user_site / "vapoursynth"), prepend=True)
        else:
            self.log_msg.emit(
                "  NOTE: git VapourSynth install not found in user "
                "site-packages — building against system vapoursynth."
            )

        self.log_msg.emit("  Configuring with meson (--prefix=~/.local)...")
        rc, out = self._run_cmd(
            ["meson", "setup", "build",
             f"--prefix={Path.home() / '.local'}", "--libdir=lib"],
            cwd=str(build_dir), timeout=180, label="meson setup bestsource",
        )
        if rc != 0:
            raise Exception(f"bestsource meson setup failed: {out[-500:]}")

        self.log_msg.emit("  Compiling BestSource (a minute or two)...")
        rc, out = self._run_cmd(
            ["ninja", "-C", "build", "-j", str(max(1, os.cpu_count() or 2))],
            cwd=str(build_dir), timeout=600, label="ninja bestsource",
        )
        if rc != 0:
            raise Exception(f"bestsource build failed: {out[-500:]}")

        rc, out = self._run_cmd(
            ["ninja", "-C", "build", "install"],
            cwd=str(build_dir), timeout=120, label="ninja install bestsource",
        )
        if rc != 0:
            raise Exception(f"bestsource install failed: {out[-500:]}")

        plugin = None
        if user_site:
            candidate = user_site / "vapoursynth" / "plugins" / "libbestsource.so"
            if candidate.exists():
                plugin = candidate
        if plugin:
            self.log_msg.emit(f"  BestSource plugin installed: {plugin}")
            self.log_msg.emit(
                "  av1an will now auto-select the fast 'bestsource' chunk "
                "method (restart the app so the probe sees it)."
            )
        else:
            self.log_msg.emit(
                "  WARNING: libbestsource.so not found at the expected "
                "user-site path — check the meson install log above."
            )

        shutil.rmtree(build_dir, ignore_errors=True)

    def _build_vapoursynth(self):
        """Clone, build, and install VapourSynth to ~/.local/ (no sudo needed)."""
        self.log_msg.emit("")
        self.log_msg.emit("=== Building VapourSynth from git ===")
        self.log_msg.emit("  Install target: ~/.local/ (no system-wide changes)")
        build_dir = Path("/tmp/vapoursynth-git-build")
        local_prefix = str(Path.home() / ".local")

        if build_dir.exists():
            self.log_msg.emit(f"  Cleaning old build directory...")
            shutil.rmtree(build_dir, ignore_errors=True)

        # Clone (shallow — faster)
        rc, out = self._run_cmd(
            ["git", "clone", "--depth", "1",
             "https://github.com/vapoursynth/vapoursynth.git",
             str(build_dir)],
            timeout=120, label="git clone vapoursynth",
        )
        if rc != 0:
            raise Exception(f"git clone VapourSynth failed: {out[-300:]}")

        # Meson setup — install to ~/.local so it doesn't touch system dirs
        self.log_msg.emit("  Configuring with meson (--prefix=~/.local)...")
        rc, out = self._run_cmd(
            ["meson", "setup", "build",
             f"--prefix={local_prefix}", "--libdir=lib"],
            cwd=str(build_dir), timeout=120, label="meson setup",
        )
        if rc != 0:
            raise Exception(f"meson setup failed: {out[-500:]}")

        # Build
        self.log_msg.emit("  Compiling VapourSynth (this may take a few minutes)...")
        rc, out = self._run_cmd(
            ["ninja", "-C", "build", "-j", str(max(1, os.cpu_count() or 2))],
            cwd=str(build_dir), timeout=900, label="ninja build",
        )
        if rc != 0:
            raise Exception(f"ninja build failed: {out[-500:]}")

        # Install to ~/.local/ — NO sudo needed (user owns this directory)
        self.log_msg.emit("  Installing VapourSynth to ~/.local/ ...")
        rc, out = self._run_cmd(
            ["ninja", "-C", "build", "install"],
            cwd=str(build_dir), timeout=120, label="ninja install",
        )
        if rc != 0:
            raise Exception(f"ninja install failed: {out[-500:]}")

        self.log_msg.emit(f"  VapourSynth installed to {local_prefix}/ (libs in {local_prefix}/lib/)")

        # Cleanup build directory
        shutil.rmtree(build_dir, ignore_errors=True)

    def _build_av1an(self):
        """Clone and build av1an from git. Installs to ~/.cargo/bin/ (no sudo needed)."""
        self.log_msg.emit("")
        self.log_msg.emit("=== Building av1an from git ===")
        self.log_msg.emit("  Install target: ~/.cargo/bin/ (no system-wide changes)")

        # Ensure cargo is in PATH
        cargo_bin = shutil.which("cargo")
        if not cargo_bin:
            # Common locations
            for p in [Path.home() / ".cargo" / "bin" / "cargo", "/usr/bin/cargo"]:
                if p.exists():
                    self._extend_env("PATH", str(p.parent))
                    cargo_bin = str(p)
                    break
        if not cargo_bin:
            raise Exception("cargo not found — cannot build av1an")

        self.log_msg.emit(f"  Using cargo at: {cargo_bin}")
        self.log_msg.emit("  Compiling av1an (this may take 10-30 minutes)...")

        rc, out = self._run_cmd(
            ["cargo", "install", "av1an",
             "--git", "https://github.com/master-of-zen/av1an",
             "--force", "--root", str(Path.home() / ".cargo")],
            timeout=3600, label="cargo install av1an",
        )
        if rc != 0:
            raise Exception(f"cargo install av1an failed: {out[-500:]}")

        new_av1an = Path.home() / ".cargo" / "bin" / "av1an"
        if new_av1an.exists():
            self.log_msg.emit(f"  av1an installed: {new_av1an}")
        else:
            self.log_msg.emit("  WARNING: av1an binary not found at expected path after build.")

    def _install_ffmpeg_build_deps(self):
        """Install ffmpeg build deps (libopus, libvorbis dev packages).

        Uses pkg-config to detect missing libraries, then installs the
        corresponding Arch/pacman packages. On other distros the user
        must install these manually; the log will name them.
        """
        self.log_msg.emit("")
        self.log_msg.emit("=== Checking ffmpeg build dependencies ===")

        # (pkg-config name, Arch package name, Debian package name)
        pkg_checks = [
            ("opus",   "opus",      "libopus-dev"),
            ("vorbis", "libvorbis", "libvorbis-dev"),
            ("ogg",    "libogg",    "libogg-dev"),
        ]
        missing_arch = []
        missing_debian = []
        for pc_name, arch_pkg, debian_pkg in pkg_checks:
            rc, _ = self._run_cmd(
                ["pkg-config", "--exists", pc_name],
                timeout=10, label=f"pkg-config {pc_name}",
            )
            if rc != 0:
                missing_arch.append(arch_pkg)
                missing_debian.append(debian_pkg)
                self.log_msg.emit(f"  Missing: {arch_pkg} (pkg-config {pc_name})")
            else:
                self.log_msg.emit(f"  OK: {pc_name}")

        if not missing_arch:
            self.log_msg.emit("  All ffmpeg build deps satisfied.")
            return

        # Try pacman (Arch) first since the rest of this app assumes Arch
        if shutil.which("pacman"):
            self.log_msg.emit(f"  Installing via pacman: {', '.join(missing_arch)}")
            rc, _ = self._sudo_cmd(
                ["pacman", "-S", "--needed", "--noconfirm"] + missing_arch,
                timeout=300, label="pacman ffmpeg-deps",
            )
            if rc != 0:
                self.log_msg.emit("  WARNING: pacman install failed — configure may fail.")
        elif shutil.which("apt-get"):
            self.log_msg.emit(f"  Installing via apt: {', '.join(missing_debian)}")
            rc, _ = self._sudo_cmd(
                ["apt-get", "install", "-y"] + missing_debian,
                timeout=300, label="apt ffmpeg-deps",
            )
            if rc != 0:
                self.log_msg.emit("  WARNING: apt install failed — configure may fail.")
        else:
            self.log_msg.emit(
                f"  No supported package manager found. Install manually: "
                f"{', '.join(missing_arch)} (Arch) or {', '.join(missing_debian)} (Debian)."
            )

    def _build_libiamf(self):
        """Clone, build, and install libiamf to ~/.local/ (no sudo needed).

        libiamf is the AOMedia Immersive Audio Model and Formats reference
        library. ffmpeg links against it via --enable-libiamf.
        """
        self.log_msg.emit("")
        self.log_msg.emit("=== Building libiamf from git ===")
        self.log_msg.emit("  Source: https://github.com/AOMediaCodec/libiamf")
        self.log_msg.emit("  Install target: ~/.local/ (no system-wide changes)")

        build_dir = Path("/tmp/libiamf-git-build")
        local_prefix = Path.home() / ".local"

        if build_dir.exists():
            shutil.rmtree(build_dir, ignore_errors=True)

        # Clone (shallow)
        self.log_msg.emit("  Cloning libiamf source (shallow)...")
        rc, out = self._run_cmd(
            ["git", "clone", "--depth", "1",
             "https://github.com/AOMediaCodec/libiamf.git",
             str(build_dir)],
            timeout=120, label="git clone libiamf",
        )
        if rc != 0:
            raise Exception(f"git clone libiamf failed: {out[-300:]}")

        # CMake configure
        cmake_build = build_dir / "build"
        cmake_build.mkdir(exist_ok=True)
        self.log_msg.emit(f"  Configuring with cmake (--prefix={local_prefix})...")
        rc, out = self._run_cmd(
            ["cmake", "-S", str(build_dir), "-B", str(cmake_build),
             f"-DCMAKE_INSTALL_PREFIX={local_prefix}",
             "-DCMAKE_BUILD_TYPE=Release",
             "-DBUILD_SHARED_LIBS=ON"],
            timeout=120, label="cmake configure libiamf",
        )
        if rc != 0:
            raise Exception(f"cmake configure libiamf failed:\n{out[-500:]}")

        # Build
        self.log_msg.emit("  Compiling libiamf...")
        rc, out = self._run_cmd(
            ["cmake", "--build", str(cmake_build), "-j",
             str(max(1, os.cpu_count() or 2))],
            timeout=600, label="cmake build libiamf",
        )
        if rc != 0:
            raise Exception(f"cmake build libiamf failed:\n{out[-500:]}")

        # Install
        self.log_msg.emit(f"  Installing libiamf to {local_prefix}/ ...")
        rc, out = self._run_cmd(
            ["cmake", "--install", str(cmake_build)],
            timeout=120, label="cmake install libiamf",
        )
        if rc != 0:
            raise Exception(f"cmake install libiamf failed:\n{out[-500:]}")

        # Make libiamf discoverable: PKG_CONFIG_PATH and LD_LIBRARY_PATH
        pc_dir = local_prefix / "lib" / "pkgconfig"
        if pc_dir.exists():
            existing_pkgs = self._build_env.get("PKG_CONFIG_PATH", "")
            if str(pc_dir) not in existing_pkgs:
                self._extend_env("PKG_CONFIG_PATH", str(pc_dir), prepend=True)
                self.log_msg.emit(f"  Added {pc_dir} to PKG_CONFIG_PATH")

        lib_dir = local_prefix / "lib"
        existing_ld = self._build_env.get("LD_LIBRARY_PATH", "")
        if str(lib_dir) not in existing_ld:
            self._extend_env("LD_LIBRARY_PATH", str(lib_dir), prepend=True)

        self.log_msg.emit(f"  libiamf installed to {local_prefix}/")

        # Cleanup
        shutil.rmtree(build_dir, ignore_errors=True)

    def _build_ffmpeg_with_iamf(self):
        """Rebuild ffmpeg from source with libiamf (and IAMF's Opus dep).

        Strategy: detect the current ffmpeg's --enable-* configure flags,
        reuse them, and append --enable-libiamf. This preserves all
        existing functionality (libsvtav1, libvpx, libx265, etc.) while
        adding IAMF support.

        Installs to ~/.local/bin/ffmpeg so it shadows the system ffmpeg
        without overwriting it. The user must restart the app for the
        new ffmpeg to take effect (probe_environment re-runs on launch).
        """
        self.log_msg.emit("")
        self.log_msg.emit("=== Building ffmpeg from git with IAMF ===")
        self.log_msg.emit("  Install target: ~/.local/bin/ (shadows system ffmpeg)")

        # 1. Detect current ffmpeg configure flags
        ffmpeg_bin = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
        self.log_msg.emit(f"  Probing current ffmpeg config: {ffmpeg_bin}")
        rc, out = self._run_cmd(
            [ffmpeg_bin, "-buildconf"],
            timeout=30, label="ffmpeg -buildconf",
        )
        if rc != 0:
            raise Exception(f"ffmpeg -buildconf failed:\n{out[-300:]}")

        # Parse --enable-* flags from output (one per line, sometimes with leading whitespace)
        enables = re.findall(r"--enable-[a-z0-9_-]+", out)
        # Dedupe while preserving order
        seen = set()
        enable_flags = []
        for e in enables:
            if e not in seen:
                seen.add(e)
                enable_flags.append(e)

        # Make sure libiamf and libopus are in the list (core requirements)
        if "--enable-libiamf" not in enable_flags:
            enable_flags.append("--enable-libiamf")
        if "--enable-libopus" not in enable_flags:
            enable_flags.append("--enable-libopus")

        self.log_msg.emit(f"  Configure flags ({len(enable_flags)}):")
        for f in enable_flags:
            self.log_msg.emit(f"    {f}")

        # 2. Clone ffmpeg source
        build_dir = Path("/tmp/ffmpeg-git-build")
        if build_dir.exists():
            shutil.rmtree(build_dir, ignore_errors=True)

        self.log_msg.emit("  Cloning ffmpeg source (shallow)...")
        rc, out = self._run_cmd(
            ["git", "clone", "--depth", "1",
             "https://git.ffmpeg.org/ffmpeg.git",
             str(build_dir)],
            timeout=300, label="git clone ffmpeg",
        )
        if rc != 0:
            # Fall back to GitHub mirror
            self.log_msg.emit("  Primary mirror failed, trying github mirror...")
            rc, out = self._run_cmd(
                ["git", "clone", "--depth", "1",
                 "https://github.com/FFmpeg/FFmpeg.git",
                 str(build_dir)],
                timeout=300, label="git clone ffmpeg (github)",
            )
            if rc != 0:
                raise Exception(f"git clone ffmpeg failed:\n{out[-300:]}")

        local_prefix = Path.home() / ".local"

        # Make sure pkg-config finds the freshly-built libiamf
        pc_dir = local_prefix / "lib" / "pkgconfig"
        existing_pkgs = self._build_env.get("PKG_CONFIG_PATH", "")
        if str(pc_dir) not in existing_pkgs:
            self._extend_env("PKG_CONFIG_PATH", str(pc_dir), prepend=True)

        # 3. Configure
        self.log_msg.emit("  Running ./configure (this may take a minute)...")
        configure_cmd = [
            "./configure",
            f"--prefix={local_prefix}",
            "--enable-shared",
            "--enable-pic",
            "--enable-version3",
        ] + enable_flags

        rc, out = self._run_cmd(
            configure_cmd,
            cwd=str(build_dir), timeout=300, label="ffmpeg configure",
        )
        if rc != 0:
            # Show the actual error — usually a missing -dev package
            raise Exception(
                "ffmpeg configure failed. This usually means a dev library\n"
                "is missing. Install the corresponding -dev package and retry.\n"
                f"Output:\n{out[-800:]}"
            )

        # 4. Build
        self.log_msg.emit("  Compiling ffmpeg (this may take 10-20 minutes)...")
        rc, out = self._run_cmd(
            ["make", "-j", str(max(1, os.cpu_count() or 2))],
            cwd=str(build_dir), timeout=2400, label="make ffmpeg",
        )
        if rc != 0:
            raise Exception(f"ffmpeg make failed:\n{out[-500:]}")

        # 5. Install to ~/.local
        self.log_msg.emit(f"  Installing ffmpeg to {local_prefix}/ ...")
        rc, out = self._run_cmd(
            ["make", "install"],
            cwd=str(build_dir), timeout=300, label="make install ffmpeg",
        )
        if rc != 0:
            raise Exception(f"make install ffmpeg failed:\n{out[-500:]}")

        # 6. Ensure ~/.local/bin is in PATH so new ffmpeg shadows system one
        local_bin = local_prefix / "bin"
        existing_path = self._build_env.get("PATH", "")
        if str(local_bin) not in existing_path:
            self._extend_env("PATH", str(local_bin), prepend=True)
            self.log_msg.emit(f"  Prepended {local_bin} to PATH (shadows system ffmpeg)")

        new_ffmpeg = local_bin / "ffmpeg"
        if new_ffmpeg.exists():
            self.log_msg.emit(f"  ffmpeg installed: {new_ffmpeg}")
            self.log_msg.emit(
                "  IMPORTANT: Restart the app for the new ffmpeg (with libiamf)\n"
                "  to be detected and used. The IAMF audio entry will then\n"
                "  be selectable (not greyed out)."
            )
        else:
            self.log_msg.emit("  WARNING: ffmpeg binary not found at expected path after build.")

        # Cleanup build dir (keep source for re-runs? No — disk is cheap, time isn't, but
        # a clean clone is more reliable than a stale tree.)
        shutil.rmtree(build_dir, ignore_errors=True)

    def stop(self):
        self._stop = True


# ──────────────────────────────────────────────
#  MAIN WINDOW (merged UI from all 3)
# ──────────────────────────────────────────────

# ──────────────────────────────────────────────
#  RETRO-FUTURISTIC MEDIA CONSOLE THEME
# ──────────────────────────────────────────────
# Brushed aluminum, amber/green LED displays,
# beveled metallic panels, VU meters, spectrum bars.
# Modernized with: rounded corners, subtle glow, glassmorphism hints,
# information-dense DAW-style layout.

MMD3_QSS = """
/* ── Global ── */
QMainWindow, QWidget#central {
    background-color: #1a1a1e;
}

/* ── Group Boxes — brushed aluminum panels ── */
QGroupBox {
    font-family: 'Segoe UI', 'Ubuntu', sans-serif;
    font-size: 10px;
    font-weight: bold;
    color: #8a8a8a;
    border: 1px solid #3a3a40;
    border-radius: 8px;
    margin-top: 14px;
    padding: 14px 10px 10px 10px;
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                               stop:0 #2c2c32, stop:0.5 #27272c, stop:1 #222228);
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 2px 10px;
    color: #666;
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                               stop:0 #2c2c32, stop:1 #222228);
    border-radius: 4px;
}

/* ── Labels ── */
QLabel {
    color: #999;
    font-size: 10px;
    font-family: 'Segoe UI', 'Ubuntu', sans-serif;
}

/* ── Line Edits — recessed aluminum wells ── */
QLineEdit {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                               stop:0 #18181c, stop:1 #141418);
    border: 1px solid #333;
    border-radius: 4px;
    padding: 5px 8px;
    color: #d4aa50;          /* amber LED */
    font-family: 'Consolas', 'DejaVu Sans Mono', 'Ubuntu Mono', monospace;
    font-size: 11px;
    selection-background-color: #d4aa50;
    selection-color: #000;
}
QLineEdit:focus {
    border-color: #d4aa50;
}

/* ── Combo Boxes ── */
QComboBox {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                               stop:0 #1e1e24, stop:1 #1a1a20);
    border: 1px solid #3a3a40;
    border-radius: 4px;
    padding: 4px 8px;
    color: #c8c8c8;
    font-family: 'Segoe UI', 'Ubuntu', sans-serif;
    font-size: 11px;
    min-height: 24px;
}
QComboBox:hover {
    border-color: #555;
}
QComboBox:focus {
    border-color: #d4aa50;
}
QComboBox::drop-down {
    border: none;
    width: 22px;
}
QComboBox::down-arrow {
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 6px solid #888;
    margin-right: 6px;
}
QComboBox QAbstractItemView {
    background: #1e1e24;
    border: 1px solid #3a3a40;
    border-radius: 4px;
    color: #c8c8c8;
    selection-background-color: #3a3a48;
    selection-color: #d4aa50;
    padding: 4px;
}
QComboBox item {
    min-height: 22px;
    padding: 2px 8px;
}

/* ── Buttons — beveled metallic (MMD3 transport style) ── */
QPushButton {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                               stop:0 #404048, stop:0.15 #38383f,
                               stop:0.85 #2e2e35, stop:1 #28282e);
    border: 1px solid #4a4a52;
    border-bottom-color: #1a1a1e;
    border-radius: 5px;
    padding: 6px 16px;
    color: #d0d0d0;
    font-family: 'Segoe UI', 'Ubuntu', sans-serif;
    font-size: 11px;
    font-weight: bold;
}
QPushButton:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                               stop:0 #4a4a54, stop:0.15 #424248,
                               stop:0.85 #363640, stop:1 #303038);
    border-color: #5a5a64;
    color: #fff;
}
QPushButton:pressed {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                               stop:0 #28282e, stop:1 #3a3a42);
    border-bottom-color: #4a4a52;
    border-top-color: #1a1a1e;
}
QPushButton:disabled {
    background: #222228;
    border-color: #2a2a30;
    color: #555;
}

/* Primary action button — amber glow */
QPushButton#btnRun {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                               stop:0 #3a3428, stop:0.15 #332e22,
                               stop:0.85 #2a261c, stop:1 #221e16);
    border: 1px solid #5a4a30;
    border-bottom-color: #1a1608;
    color: #d4aa50;
    font-size: 13px;
    letter-spacing: 2px;
}
QPushButton#btnRun:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                               stop:0 #4a4030, stop:0.15 #423828,
                               stop:0.85 #3a3020, stop:1 #322a1a);
    border-color: #d4aa50;
    color: #f0d080;
}
QPushButton#btnRun:disabled {
    background: #22201a;
    border-color: #2a2820;
    color: #5a4a30;
}

/* Stop button — red danger */
QPushButton#btnStop {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                               stop:0 #3a2222, stop:0.15 #321c1c,
                               stop:0.85 #2a1616, stop:1 #221010);
    border: 1px solid #5a3030;
    border-bottom-color: #1a0808;
    color: #e05050;
    font-size: 13px;
    letter-spacing: 2px;
}
QPushButton#btnStop:hover {
    border-color: #e05050;
    color: #ff7070;
}
QPushButton#btnStop:disabled {
    background: #221a1a;
    border-color: #2a2020;
    color: #5a3030;
}

/* Rebuild-from-git button — muted teal */
QPushButton#btnRebuild {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                               stop:0 #1e2e2e, stop:0.15 #1a2a2a,
                               stop:0.85 #162424, stop:1 #121e1e);
    border: 1px solid #2a5050;
    border-bottom-color: #0e1818;
    color: #50b0b0;
    font-size: 10px;
    letter-spacing: 1px;
}
QPushButton#btnRebuild:hover {
    border-color: #50b0b0;
    color: #70d0d0;
}
QPushButton#btnRebuild:disabled {
    background: #1a1e1e;
    border-color: #222828;
    color: #304040;
}

/* Browse buttons — small, subdued */
QPushButton#btnBrowse {
    font-size: 9px;
    padding: 4px 10px;
    letter-spacing: 1px;
}

/* ── Check Boxes ── */
QCheckBox {
    color: #999;
    font-size: 10px;
    spacing: 8px;
    font-family: 'Segoe UI', 'Ubuntu', sans-serif;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border-radius: 3px;
    border: 1px solid #444;
    background: #1a1a1e;
}
QCheckBox::indicator:checked {
    background: #d4aa50;
    border-color: #b8903a;
}
QCheckBox#dangerCheck {
    color: #c05050;
    font-weight: bold;
}
QCheckBox#dangerCheck::indicator:checked {
    background: #c04040;
    border-color: #a03030;
}

/* ── Text Edit (log) — LED terminal display ── */
QTextEdit#logBox {
    background: #0a0a0c;
    border: 2px solid #1e1e24;
    border-radius: 6px;
    color: #40d060;          /* green phosphor LED */
    font-family: 'Consolas', 'DejaVu Sans Mono', 'Ubuntu Mono', monospace;
    font-size: 11px;
    padding: 8px;
}

/* ── Status Bar — LED readout strip ── */
QStatusBar {
    background: #0e0e12;
    border-top: 1px solid #2a2a30;
    font-family: 'Consolas', 'DejaVu Sans Mono', 'Ubuntu Mono', monospace;
    font-size: 10px;
    color: #d4aa50;
    padding: 2px 8px;
}
QStatusBar QLabel {
    color: #d4aa50;
    font-family: 'Consolas', 'DejaVu Sans Mono', 'Ubuntu Mono', monospace;
    font-size: 10px;
}

/* ── Tooltips ── */
QToolTip {
    background: #2a2a30;
    color: #c8c8c8;
    border: 1px solid #444;
    border-radius: 4px;
    padding: 6px;
    font-size: 10px;
}

/* ── Scrollbars — thin, dark ── */
QScrollBar:vertical {
    background: #141418;
    width: 10px;
    border-radius: 5px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: #3a3a42;
    border-radius: 5px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background: #4a4a54;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar:horizontal {
    background: #141418;
    height: 10px;
    border-radius: 5px;
}
QScrollBar::handle:horizontal {
    background: #3a3a42;
    border-radius: 5px;
    min-width: 30px;
}
QScrollBar::handle:horizontal:hover {
    background: #4a4a54;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0;
}
"""


class OpenCodecMaster(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("OpenTranscode — dcos.net")
        self.resize(1100, 920)
        self.worker: EncoderWorker | None = None
        # v4.7.0: hybrid (GPU + CPU lanes) bookkeeping. *workers* holds
        # every active lane; _hybrid_pending/_hybrid_totals aggregate the
        # per-lane finished_queue signals into one summary.
        self.workers: list[EncoderWorker] = []
        self._hybrid_pending = 0
        self._hybrid_totals = [0, 0]

        self._pending_deletes: list[Path] = []

        self._apply_mmd3_theme()
        self._build_ui()

        # Probe environment after UI is up
        QTimer.singleShot(500, self._probe_and_init)

    # ── UI Construction ──

    def _build_ui(self):
        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 6, 10, 4)
        root.setSpacing(4)

        # ── Header ──
        header = QWidget()
        header_lay = QVBoxLayout(header)
        header_lay.setContentsMargins(0, 0, 0, 0)
        header_lay.setSpacing(0)

        title = QLabel("OpenTranscode")
        title.setFont(QFont("Segoe UI", 22, QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("color: #d4aa50; letter-spacing: 4px;")
        header_lay.addWidget(title)

        subtitle = QLabel('dcos.net  //  concurrent open-source transcoding')
        subtitle.setFont(QFont("Consolas", 8))
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setStyleSheet("color: #555; letter-spacing: 2px;")
        header_lay.addWidget(subtitle)

        accent = QWidget()
        accent.setFixedHeight(1)
        accent.setStyleSheet("background: qlineargradient(x1:0, y1:0, x2:1, y2:0,"
                               "stop:0 transparent, stop:0.15 #d4aa5044,"
                               "stop:0.5 #d4aa5088, stop:0.85 #d4aa5044, stop:1 transparent);")
        header_lay.addWidget(accent)

        root.addWidget(header)

        # ── Paths ──
        path_grp = QGroupBox("Paths")
        path_lay = QVBoxLayout(path_grp)
        path_lay.setSpacing(2)
        path_lay.setContentsMargins(10, 14, 10, 8)

        self.in_path_edit = QLineEdit(str(Path.home() / "Videos" / "INCOMING"))
        self.out_path_edit = QLineEdit(str(Path.home() / "Videos" / "ARCHIVE"))
        for label_text, line_edit in [
            ("IN:", self.in_path_edit),
            ("OUT:", self.out_path_edit),
        ]:
            row = QHBoxLayout()
            row.setSpacing(6)
            lbl = QLabel(label_text)
            lbl.setFixedWidth(28)
            lbl.setStyleSheet("color: #d4aa50; font-family: 'Consolas', monospace; font-weight: bold; font-size: 10px;")
            row.addWidget(lbl)
            row.addWidget(line_edit, 1)
            btn_browse = QPushButton("...")
            btn_browse.setObjectName("btnBrowse")
            btn_browse.setFixedSize(30, 22)
            btn_browse.setToolTip("Browse")
            btn_browse.clicked.connect(
                lambda checked, le=line_edit, is_dir=True: self._browse(le, is_dir)
            )
            row.addWidget(btn_browse)
            path_lay.addLayout(row)

        root.addWidget(path_grp)

        # ── Encoder Chain ──
        codec_grp = QGroupBox("Encoder Chain")
        codec_lay = QHBoxLayout(codec_grp)
        codec_lay.setSpacing(8)
        codec_lay.setContentsMargins(10, 14, 10, 8)

        for col_idx, (label, combo_items, slot) in enumerate([
            ("VIDEO", [vc.label for vc in VIDEO_CODECS], self._on_codec_changed),
            ("PRESET", [], None),
            ("AUDIO", [ap.label for ap in AUDIO_PROFILES], self._on_audio_changed),
            ("CONTAINER", [cp.label for cp in CONTAINER_PROFILES], self._on_container_changed),
            ("RESOLUTION", [], self._on_resolution_changed),
            ("SUBS", [so[0] for so in SUBTITLE_OPTIONS], None),
        ]):
            col = QVBoxLayout()
            col.setSpacing(1)
            lbl = QLabel(label)
            lbl.setStyleSheet("color: #666; font-size: 7px; letter-spacing: 1px;")
            col.addWidget(lbl)

            combo = QComboBox()
            combo.setFixedHeight(24)
            if combo_items:
                combo.addItems(combo_items)
            if slot:
                combo.currentIndexChanged.connect(slot)
            col.addWidget(combo)
            codec_lay.addLayout(col)

            if label == "VIDEO":
                self.codec_combo = combo
            elif label == "PRESET":
                self.preset_combo = combo
                self._populate_presets(0)
                self.preset_combo.setCurrentIndex(1)
            elif label == "AUDIO":
                self.audio_combo = combo
            elif label == "CONTAINER":
                self.container_combo = combo
            elif label == "RESOLUTION":
                self.resolution_combo = combo
                self._populate_resolution_combo()
            elif label == "SUBS":
                self.subs_combo = combo

        root.addWidget(codec_grp)

        # ── Side panel: compact knobs ──
        knobs_panel = QWidget()
        knobs_panel.setFixedWidth(170)
        knobs_lay = QVBoxLayout(knobs_panel)
        knobs_lay.setContentsMargins(6, 8, 6, 8)
        knobs_lay.setSpacing(6)
        knobs_lay.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        # CRF Knob — amber
        self.crf_knob = RadioKnob(
            min_val=18, max_val=52, default_val=32,
            label="Quality",
            unit="CRF",
            color=(212, 170, 80),
            num_ticks=18,
            tick_labels=["18", "28", "38", "52"],
            snap_ticks=True,
            compact=True,
        )
        self.crf_knob.valueChanged.connect(self._on_crf_knob_changed)
        knobs_lay.addWidget(self.crf_knob, 0, Qt.AlignmentFlag.AlignHCenter)

        # Volume Knob — green (dual-pass loudnorm target)
        self.vol_knob = RadioKnob(
            min_val=-20.0, max_val=6.0, default_val=0.0,
            label="LUFS",
            unit="dB",
            color=(64, 208, 96),
            num_ticks=27,
            tick_labels=["-20", "-10", "0", "+6"],
            snap_ticks=True,
            compact=True,
        )
        self.vol_knob.setToolTip(
            "Dual-pass audio normalization target (EBU R128 LUFS).\n"
            "0 = off (pass-through).\n"
            "Each file is analyzed individually: loudnorm measures its\n"
            "current LUFS and true peak, then computes the exact gain\n"
            "to hit this target. If the gain would push peaks above\n"
            "-1.5 dBTP, gain is reduced to keep 15%% headroom.\n"
            "Common targets: -14 (streaming), -16 (broadcast), -23 (cinema)."
        )
        self.vol_knob.valueChanged.connect(self._on_vol_knob_changed)
        knobs_lay.addWidget(self.vol_knob, 0, Qt.AlignmentFlag.AlignHCenter)

        # ── Options row ──
        opt_row = QHBoxLayout()
        opt_row.setSpacing(8)
        opt_lbl = QLabel("FILTER")
        opt_lbl.setFixedWidth(44)
        opt_lbl.setStyleSheet("color: #666; font-size: 7px; letter-spacing: 1px;")
        opt_row.addWidget(opt_lbl)
        self.ext_edit = QLineEdit(", ".join(sorted(DEFAULT_INPUT_EXTENSIONS)))
        self.ext_edit.setFixedHeight(22)
        self.ext_edit.setToolTip("File extensions to process. Separate with commas.")
        opt_row.addWidget(self.ext_edit)

        self.del_check = QCheckBox("Delete source after verify")
        self.del_check.setObjectName("dangerCheck")
        self.del_check.setToolTip(
            "Sources are only deleted after all files finish and cleanup passes.\n"
            "If any file fails, you will be prompted before deletion."
        )
        opt_row.addWidget(self.del_check)

        # v5-01: Force checkbox — skip ffprobe validation and attempt encode
        # even for files ffprobe cannot read. Use for the rare edge case where
        # ffprobe fails but the file is actually valid. Default OFF — most
        # "ffprobe can't read" files are genuinely invalid (failed downloads,
        # HTML saved as .mp4, truncated files, etc.).
        self.force_check = QCheckBox("Force (skip validation)")
        self.force_check.setToolTip(
            "Skip ffprobe pre-validation and attempt encode even for files\n"
            "ffprobe cannot read. Useful for the rare case where ffprobe\n"
            "fails but the file is actually valid (rare codec, broken\n"
            "container metadata). WARNING: with this enabled, invalid files\n"
            "(failed downloads, HTML, truncated) will waste the full\n"
            "per-file timeout before failing."
        )
        opt_row.addWidget(self.force_check)

        # v4.4.3: av1an toggle — UI equivalent of --use-av1an.
        self.av1an_check = QCheckBox("av1an (chunk-parallel)")
        self.av1an_check.setToolTip(
            "Use av1an chunk-parallel encoding instead of single-pass ffmpeg.\n"
            "Faster on multi-core machines WITH working VapourSynth setup,\n"
            "but more fragile (y4m pipe breaks, concat failures on phone\n"
            "videos with sparse keyframes). Default OFF = ffmpeg-only,\n"
            "which is more reliable across distros."
        )
        opt_row.addWidget(self.av1an_check)

        # v4.6.0: ENGINE selector — Auto (GPU if available) / GPU / CPU.
        # Auto uses the NVENC hardware encoder for the selected codec
        # family when the environment probe's live encode test proved it
        # works (av1 → av1_nvenc on RTX 40+, hevc → hevc_nvenc on every
        # NVENC generation); otherwise it stays on the CPU encoders. GPU
        # mode runs through the single-pass ffmpeg path — av1an cannot
        # drive NVENC, and one NVENC process outruns chunk-parallel CPU
        # workers anyway.
        self.engine_combo = QComboBox()
        self.engine_combo.addItems([
            "Engine: Auto (GPU if available)",
            "Engine: GPU (NVENC)",
            "Engine: CPU",
            "Engine: Hybrid (GPU + CPU)",
        ])
        self.engine_combo.setToolTip(
            "Video encode engine.\n\n"
            "Auto — use the NVENC GPU encoder when the selected codec\n"
            "   family has one AND a live encode test proved it works on\n"
            "   this system; fall back to CPU otherwise (default).\n"
            "GPU — force NVENC (hevc_nvenc / av1_nvenc); falls back to\n"
            "   CPU with a log message when unavailable. GPU encodes run\n"
            "   via single-pass ffmpeg (av1an chunking is not used).\n"
            "CPU — force the software encoders (SVT-AV1 / VP9 / x265).\n"
            "Hybrid — run BOTH at once: the queue is split between a\n"
            "   GPU lane and a CPU lane (balanced by file size), so the\n"
            "   CPU cores are not idle while NVENC encodes. With av1an\n"
            "   enabled the CPU lane uses chunk-parallel too. GPU-lane\n"
            "   files get NVENC quality; CPU-lane files get software-\n"
            "   encoder quality. Needs 2+ encodable files.\n"
        )
        self.engine_combo.setFixedHeight(24)
        # v4.7.1: self.env is None (or absent) until _probe_and_init
        # runs AFTER _build_ui — the pre-select must not touch it here.
        _engine_flags = getattr(getattr(self, "env", None), "av1an_flags", None) or {}
        _engine_pref = _engine_flags.get("engine")
        _engine_index = {"auto": 0, "gpu": 1, "cpu": 2, "hybrid": 3}
        self.engine_combo.setCurrentIndex(_engine_index.get(_engine_pref, 0))
        # v4.8.0: GPU capability-profile dropdown. Entries combine whole
        # card generations (same silicon = same encoding behaviour) and
        # include data-center + crypto-era oddballs. "Auto-detect" uses
        # the probe's name match; a specific profile forces the API even
        # when auto-match fails. The rebuild-from-git dep tree extends
        # with the selected profile's packages.
        self.gpu_combo = QComboBox()
        self.gpu_combo.addItem("GPU: Auto-detect")
        for _gp in GPU_PROFILES:
            self.gpu_combo.addItem(f"GPU: {_gp.label}")
        self.gpu_combo.setToolTip(
            "Hardware encoder capability class.\n"
            "Auto-detect matches your card via nvidia-smi/lspci and the\n"
            "live encode probe decides what actually works. Forcing a\n"
            "profile also extends the REBUILD FROM GIT dependency tree\n"
            "with that GPU's packages (nv-codec-headers / VAAPI / QSV)."
        )
        self.gpu_combo.setFixedHeight(24)
        _gpu_pref = _engine_flags.get("gpu_profile")
        _gpu_keys = ["auto"] + [_gp.key for _gp in GPU_PROFILES]
        self.gpu_combo.setCurrentIndex(
            _gpu_keys.index(_gpu_pref) if _gpu_pref in _gpu_keys else 0
        )
        self.gpu_combo.currentIndexChanged.connect(self._on_gpu_profile_changed)
        # v4.8.1: ENGINE = CPU makes the GPU choice inert — grey it out.
        # The "None (CPU-only encode)" entry stays available for users
        # who have a GPU in the box but don't want encoding on it.
        self.engine_combo.currentIndexChanged.connect(self._on_engine_changed)
        self._update_gpu_combo_state()
        _gpu_lay = QVBoxLayout()
        _gpu_lay.setSpacing(1)
        _gpu_lbl = QLabel("GPU")
        _gpu_lbl.setStyleSheet("color: #666; font-size: 7px; letter-spacing: 1px;")
        _gpu_lay.addWidget(_gpu_lbl)
        _gpu_lay.addWidget(self.gpu_combo)
        opt_row.addLayout(_gpu_lay)
        _engine_lay = QVBoxLayout()
        _engine_lay.setSpacing(1)
        _engine_lbl = QLabel("ENGINE")
        _engine_lbl.setStyleSheet("color: #666; font-size: 7px; letter-spacing: 1px;")
        _engine_lay.addWidget(_engine_lbl)
        _engine_lay.addWidget(self.engine_combo)
        opt_row.addLayout(_engine_lay)
        root.addLayout(opt_row)

        # ── Log + Knobs: horizontal split ──
        mid_split = QHBoxLayout()
        mid_split.setSpacing(6)

        # Log: LED terminal (takes remaining space)
        self.log_box = QTextEdit()
        self.log_box.setObjectName("logBox")
        self.log_box.setReadOnly(True)
        mid_split.addWidget(self.log_box, 1)

        # Knobs panel on the right
        mid_split.addWidget(knobs_panel)

        root.addLayout(mid_split, 1)

        # ── Status Bar: LED readout ──
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status_label = QLabel("  INITIALIZING...")
        self.status_label.setStyleSheet(
            "color: #d4aa50; font-family: 'Consolas', 'DejaVu Sans Mono', monospace; font-size: 10px;"
        )
        self.status.addWidget(self.status_label, 1)

        # ── Transport Buttons ──
        btn_lay = QHBoxLayout()
        btn_lay.setSpacing(8)

        self.btn_run = QPushButton("  >  ENCODE")
        self.btn_run.setObjectName("btnRun")
        self.btn_run.setFixedHeight(40)
        self.btn_run.setEnabled(False)
        self.btn_run.clicked.connect(self._start_process)
        btn_lay.addWidget(self.btn_run)

        self.btn_stop = QPushButton("  []  STOP")
        self.btn_stop.setObjectName("btnStop")
        self.btn_stop.setFixedHeight(40)
        self.btn_stop.clicked.connect(self._stop_process)
        self.btn_stop.setEnabled(False)
        btn_lay.addWidget(self.btn_stop)

        self.btn_rebuild = QPushButton("  <>  REBUILD FROM GIT")
        self.btn_rebuild.setObjectName("btnRebuild")
        self.btn_rebuild.setFixedHeight(40)
        self.btn_rebuild.setToolTip(
            "Compile VapourSynth + av1an + BestSource from git source.\n"
            "Resolves ABI/version mismatch when package managers\n"
            "install incompatible versions. ALWAYS available — even on a\n"
            "bare system: it first generates its own dependency tree\n"
            "(installs missing build tools + zimg via the distro package\n"
            "manager, one privilege prompt) and then builds everything\n"
            "into ~/.local / ~/.cargo (no system changes)."
        )
        self.btn_rebuild.clicked.connect(self._manual_rebuild)
        # v4.7.1: ALWAYS usable — the build generates its own dependency
        # tree, so it must not depend on a successful probe (a failed
        # probe is exactly when you need it).
        btn_lay.addWidget(self.btn_rebuild)

        self.btn_about = QPushButton("  ?  ABOUT / LICENSES")
        self.btn_about.setObjectName("btnAbout")
        self.btn_about.setFixedHeight(40)
        self.btn_about.setToolTip(
            "Show open-source license attributions for all\n"
            "third-party components invoked by this application."
        )
        self.btn_about.clicked.connect(self._show_license_dialog)
        btn_lay.addWidget(self.btn_about)
        root.addLayout(btn_lay)

        # ── Footer ──
        footer = QWidget()
        footer_lay = QHBoxLayout(footer)
        footer_lay.setContentsMargins(6, 4, 6, 2)
        footer_lay.setSpacing(0)

        link_lbl = QLabel(
            '<a href="http://git.dcos.net/dcosnet/OpenTranscode" '
            'style="color: #888; text-decoration: none;">Visit Homepage</a>'
        )
        link_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        link_lbl.setOpenExternalLinks(True)
        link_lbl.setStyleSheet("font-size: 8px;")
        footer_lay.addWidget(link_lbl)

        footer_lay.addStretch()

        copy_lbl = QLabel(
            'AGPL-3.0  |  Jeremy Anderson - <a href="http://dcos.net" '
            'style="color: #888; text-decoration: none;">dcos.net</a>  (c) 2026'
        )
        copy_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        copy_lbl.setOpenExternalLinks(True)
        copy_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        copy_lbl.setStyleSheet("color: #555; font-size: 8px;")
        footer_lay.addWidget(copy_lbl)

        root.addWidget(footer)

    def _apply_mmd3_theme(self):
        self.setStyle(QStyleFactory.create("Fusion"))
        self.setStyleSheet(MMD3_QSS)
        # Palette as fallback for things QSS doesn't cover
        p = QPalette()
        p.setColor(QPalette.ColorRole.Window,          QColor(26, 26, 30))
        p.setColor(QPalette.ColorRole.WindowText,      QColor(200, 200, 200))
        p.setColor(QPalette.ColorRole.Base,            QColor(20, 20, 24))
        p.setColor(QPalette.ColorRole.AlternateBase,   QColor(40, 40, 46))
        p.setColor(QPalette.ColorRole.ToolTipBase,     QColor(30, 30, 36))
        p.setColor(QPalette.ColorRole.ToolTipText,     QColor(200, 200, 200))
        p.setColor(QPalette.ColorRole.Text,            QColor(200, 200, 200))
        p.setColor(QPalette.ColorRole.Button,          QColor(40, 40, 46))
        p.setColor(QPalette.ColorRole.ButtonText,      QColor(200, 200, 200))
        p.setColor(QPalette.ColorRole.Highlight,       QColor(212, 170, 80))
        p.setColor(QPalette.ColorRole.HighlightedText, QColor(0, 0, 0))
        QApplication.instance().setPalette(p)

    # ── Slots ──

    @Slot()
    def _on_codec_changed(self, idx: int):
        self._populate_presets(idx)
        profile = VIDEO_CODECS[idx]
        lo, hi = profile.crf_range
        self.crf_knob.min_val = lo
        self.crf_knob.max_val = hi
        self.crf_knob.setValue(float(profile.default_crf))
        # Auto-select best container via index lookup — no for-loop, no break.
        # next(..., None) returns the first match or None; the if guards the
        # block so we only touch container_combo when a match was found.
        match = next(
            (i for i, cp in enumerate(CONTAINER_PROFILES)
             if cp.ext == profile.container),
            None,
        )
        if match is not None:
            self.container_combo.blockSignals(True)
            self.container_combo.setCurrentIndex(match)
            self.container_combo.blockSignals(False)
        # Re-evaluate compatibility after auto-container change.
        self._check_combo_compatibility()

    def _populate_presets(self, codec_idx: int):
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        if 0 <= codec_idx < len(VIDEO_CODECS):
            self.preset_combo.addItems(VIDEO_CODECS[codec_idx].presets)
        self.preset_combo.blockSignals(False)

    @Slot()
    def _on_container_changed(self, idx: int):
        if idx >= 0:
            ext = CONTAINER_PROFILES[idx].ext
            self._log(f"Container set to: {ext}")
        self._check_combo_compatibility()

    @Slot()
    def _on_audio_changed(self, idx: int):
        if idx >= 0:
            self._log(f"Audio set to: {AUDIO_PROFILES[idx].label}")
        self._check_combo_compatibility()

    def _check_combo_compatibility(self) -> list[str]:
        """Check current video/audio/container combination for known
        incompatibilities. Logs every warning and returns the full list
        (empty if clean). Hard incompatibilities (which would fail at
        encode/mux time) are prefixed ``INCOMPATIBLE:`` and also block
        the Start button via _start_process. Soft warnings are prefixed
        ``WARNING:`` and only appear in the log.

        Safe to call during __init__ — every attribute is guarded.

        Refactored to table-driven dispatch: every rule is a tuple of
        (predicate, severity, message-fn), evaluated by a single loop.
        Adding a new rule is a one-line table change; no nested ifs.

        SEI CERT STR09-C spirit: predicates return plain bool, never None;
        messages are produced only when their predicate fires, so the
        severity prefix is always consistent with the predicate outcome.
        """
        # Resolve current selection with full defensive validation.
        # All four early returns return the same value ([]), so this
        # block reads as a flat guard rather than a nested decision tree.
        if not all(hasattr(self, attr) for attr in
                   ("codec_combo", "audio_combo", "container_combo")):
            return []

        codec_idx = self.codec_combo.currentIndex()
        audio_idx = self.audio_combo.currentIndex()
        container_idx = self.container_combo.currentIndex()

        if min(codec_idx, audio_idx, container_idx) < 0:
            return []

        if not (codec_idx < len(VIDEO_CODECS)
                and audio_idx < len(AUDIO_PROFILES)
                and container_idx < len(CONTAINER_PROFILES)):
            return []

        video_codec = VIDEO_CODECS[codec_idx]
        audio_profile = AUDIO_PROFILES[audio_idx]
        container = CONTAINER_PROFILES[container_idx]

        # ── Compatibility rule table ──
        # Each rule: (predicate, severity, message)
        # predicate: callable(video_codec, audio_profile, container) -> bool
        # severity:  "INCOMPATIBLE" or "WARNING"
        # message:   str (already-formatted)
        #
        # To add a new rule, append a tuple here. No code below changes.
        def _is_hevc(vc, _ap, c) -> bool:
            return vc.ffmpeg_encoder == "libx265" and c.ext == "webm"

        # v3 (OTC-012, SEI CERT STR09-C): compare against the
        # AudioProfile.ffmpeg_encoder_name field directly, not via
        # substring match on params (which could false-match a
        # hypothetical `-libiamf-mode` argument).
        def _is_iamf_non_mp4(_vc, ap, c) -> bool:
            return ap.ffmpeg_encoder_name == "libiamf" and c.ext != "mp4"

        def _is_vorbis_in_mp4(_vc, ap, c) -> bool:
            return ap.ffmpeg_encoder_name == "libvorbis" and c.ext == "mp4"

        def _is_flac_in_webm(_vc, ap, c) -> bool:
            return ap.ffmpeg_encoder_name == "flac" and c.ext == "webm"

        def _is_vp9_in_mp4(vc, _ap, c) -> bool:
            return vc.ffmpeg_encoder == "libvpx-vp9" and c.ext == "mp4"

        rules: tuple[tuple, ...] = (
            (_is_hevc,           "INCOMPATIBLE",
             "x265 (HEVC) cannot be muxed into WebM. Use MKV or MP4 instead."),
            (_is_iamf_non_mp4,   "INCOMPATIBLE",
             f"IAMF audio requires the MP4 container — cannot mux into "
             f"{container.ext.upper()}. Switch container to MP4."),
            (_is_vorbis_in_mp4,  "WARNING",
             "Vorbis in MP4 has limited player support. Consider Opus or MKV/WebM."),
            (_is_flac_in_webm,   "WARNING",
             "FLAC in WebM is rarely supported by players. Consider MKV instead."),
            (_is_vp9_in_mp4,     "WARNING",
             "VP9 in MP4 has uneven player support. WebM is the canonical VP9 container."),
        )

        # Single-pass evaluation: build the warnings list by filtering
        # the rule table through each predicate. No nested if/elif.
        warnings: list[str] = [
            f"{severity}: {message}"
            for predicate, severity, message in rules
            if predicate(video_codec, audio_profile, container)
        ]

        for w in warnings:
            self._log(w)

        return warnings

    def _populate_resolution_combo(self):
        """Populate resolution dropdown with separator headers per category.

        Refactored with PEP 634/868 structural pattern matching: the
        category-transition decision is expressed as a single match
        statement instead of nested ifs. The match value is a 2-tuple
        of (current_category, previous_category); each case is a flat
        pattern, no nesting.
        """
        # Maps combo box position -> RESOLUTION_PRESETS index.
        # Separators occupy combo positions too, so we must track them.
        self._res_preset_indices: dict[int, int] = {}
        last_cat: str | None = None
        combo_pos = 0

        for i, rp in enumerate(RESOLUTION_PRESETS):
            # Single-level decision: insert separator only when transitioning
            # to a new category AND we are not on the first category.
            match (rp.category, last_cat):
                case (cat, prev) if cat != prev and prev is not None:
                    self.resolution_combo.insertSeparator(combo_pos)
                    combo_pos += 1  # separator takes a slot

            last_cat = rp.category
            self.resolution_combo.addItem(rp.label)
            self._res_preset_indices[combo_pos] = i
            combo_pos += 1

    def _get_current_resolution(self) -> ResolutionProfile:
        """Get the ResolutionProfile for the current combo selection, handling separators."""
        combo_idx = self.resolution_combo.currentIndex()
        preset_i = self._res_preset_indices.get(combo_idx)
        if preset_i is not None:
            return RESOLUTION_PRESETS[preset_i]
        return RESOLUTION_PRESETS[0]

    @Slot()
    def _on_resolution_changed(self, idx: int):
        rp = self._get_current_resolution()
        if rp.width is not None:
            self._log(
                f"Resolution: {rp.width}x{rp.height} ({rp.aspect_label}) — "
                f"files will be pre-scaled with ffmpeg before encoding."
            )
        else:
            self._log("Resolution: Original (no scaling).")

    @Slot(float)
    def _on_crf_knob_changed(self, val: float):
        direction = "higher quality" if val < 28 else ("balanced" if val < 38 else "smaller file")
        self._log(f"CRF: {val:.0f} ({direction})")

    @Slot(float)
    def _on_vol_knob_changed(self, val: float):
        if abs(val) < 0.01:
            self._log("Audio normalization: OFF (pass-through)")
        else:
            direction = "louder" if val > 0 else "quieter"
            self._log(f"Audio normalization: {val:+.1f} dB ({direction})")


    @Slot()
    def _browse(self, line_edit: QLineEdit, is_dir: bool = True):
        if is_dir:
            path = QFileDialog.getExistingDirectory(self, "Select Directory")
            if path:
                line_edit.setText(path)

    def _log(self, msg: str):
        # Guard against signals (combo currentIndexChanged, knob valueChanged,
        # etc.) firing during __init__ before self.log_box has been
        # constructed. Without this, the first addItem() on any combo
        # triggers its slot, which calls _log(), which dereferences
        # self.log_box while it is still None -> AttributeError -> crashes
        # the app on launch. Also buffer messages so they aren't lost.
        if not hasattr(self, "log_box") or self.log_box is None:
            buffered = getattr(self, "_log_buffer", None)
            if buffered is None:
                buffered = self._log_buffer = []
            buffered.append(msg)
            return
        # Flush any messages that arrived before log_box existed.
        buffered = getattr(self, "_log_buffer", None)
        if buffered:
            for m in buffered:
                self.log_box.append(f"> {m}")
            self._log_buffer = []
        self.log_box.append(f"> {msg}")
        sb = self.log_box.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ── Environment Probe ──

    def _probe_and_init(self):
        self.env = probe_environment()

        # --- Distro banner ---
        distro = self.env.distro
        self._log(f"Distro: {distro.name} (family={distro.family}, v{distro.version_id})")
        self._log(f"Package manager: {distro.pkg_manager}")

        # --- Warnings (info-level, not errors) ---
        for w in self.env.warnings:
            self._log(f"INFO: {w}")

        # --- Hard errors ---
        if not self.env.av1an_path:
            self._log("CRITICAL: 'av1an' not found in PATH or distro-specific paths.")
            if self.env.install_hint:
                self._log(f"  TRY: {self.env.install_hint}")
            self.status_label.setText(f"NOT READY — missing av1an ({distro.family})")
            return
        if not self.env.ffmpeg_path:
            self._log("CRITICAL: 'ffmpeg' not found in PATH or distro-specific paths.")
            if self.env.install_hint:
                self._log(f"  TRY: {self.env.install_hint}")
            self.status_label.setText(f"NOT READY — missing ffmpeg ({distro.family})")
            return

        if self.env.errors:
            for e in self.env.errors:
                self._log(f"ERROR: {e}")

        # If there are still errors after logging (e.g. missing runtime deps), block start
        if self.env.errors:
            dep_count = len(self.env.missing_dep_pkgs)
            if dep_count:
                self.status_label.setText(
                    f"NOT READY — {dep_count} runtime dep(s) missing. See log."
                )
                return

        # --- Probe results ---
        flag_info = ", ".join(f"{k}={v}" for k, v in self.env.av1an_flags.items() if k != "has_chunk_method" and k != "has_scenes")
        self._log(f"av1an: {self.env.av1an_path} (v{self.env.av1an_version or '?'})")
        if flag_info:
            self._log(f"  Flags: {flag_info}")

        if self.env.ffmpeg_version:
            self._log(f"ffmpeg: {self.env.ffmpeg_path} (v{self.env.ffmpeg_version})")

        # --- FFmpeg encoder library summary (audio-relevant only for our purposes) ---
        available_libs = [name for name, present in self.env.ffmpeg_libs.items() if present]
        missing_audio = [name for name, present in self.env.ffmpeg_libs.items()
                         if not present and name in ("libopus", "libvorbis", "flac")]
        if available_libs:
            self._log(f"  FFmpeg encoders available: {', '.join(available_libs)}")
        if missing_audio:
            self._log(f"  FFmpeg audio encoders MISSING: {', '.join(missing_audio)}")
            self._log(f"  Some audio codec options may fail. Check distro package: {distro.ffmpeg_pkg}")

        # --- Disable unavailable codec options in UI ---
        self._disable_unavailable_codecs()

        # v4.1.0: show the intelligent worker-count math in the env-probe
        # banner so the user can verify the thread budget before clicking
        # START. The same math runs again in EncoderWorker.run() to set
        # the actual values used per-encode.
        cpu = self.env.cpu
        # Read --max-workers / --threads-per-worker overrides from
        # env.av1an_flags (set by cli.main before launch_gui runs).
        cli_max_workers = (
            self.env.av1an_flags.get("max_workers")
            if isinstance(self.env.av1an_flags.get("max_workers"), int)
            else None
        )
        cli_tpw = (
            self.env.av1an_flags.get("threads_per_worker")
            if isinstance(self.env.av1an_flags.get("threads_per_worker"), int)
            else None
        )
        wc, tpw = _compute_intelligent_worker_count_for(
            self.env, max_workers=cli_max_workers,
            threads_per_worker_override=cli_tpw,
        )
        active = wc * tpw
        reserved = max(0, cpu.logical_threads - active)
        self._log(
            f"Chunk-parallel mode: {wc} workers × {tpw} threads = {active} active "
            f"({cpu.physical_cores} physical cores, {cpu.logical_threads} logical, "
            f"{cpu.threads_per_core}T/core — {reserved} reserved for OS/UI)"
        )

        self.btn_run.setEnabled(True)
        self.btn_run.setText("START PROCESSING")
        self.btn_rebuild.setEnabled(True)  # available after successful probe
        vs_info = f" | VS{self.env.vs_version}" if self.env.vs_version else ""
        # Show ffmpeg video encoder availability (for fallback)
        fb_encs = []
        for vc in VIDEO_CODECS:
            lib_key = ffmpeg_lib_key_for(vc.ffmpeg_encoder)  # v3: OTC-007
            if self.env.ffmpeg_libs.get(lib_key, False):
                fb_encs.append(vc.ffmpeg_encoder)
        fb_info = f" | ffmpeg-fb:{'+'.join(fb_encs)}" if fb_encs else ""
        self.status_label.setText(
            f"{distro.name} | {cpu.physical_cores}C/{cpu.logical_threads}T | "
            f"av1an v{self.env.av1an_version or '?'} | ffmpeg v{self.env.ffmpeg_version or '?'}{vs_info}{fb_info}"
        )

        # --- License attribution banner (shown once after successful probe) ---
        # POSIX-friendly: log plain text, no escape codes, no decorative box chars
        # that might confuse terminals. Each tool is named with its SPDX id so
        # the user can audit obligations at a glance.
        self._show_license_banner()

    def _show_license_banner(self) -> None:
        """Log the active-component license summary once at startup.

        SEI CERT MSC04-C: license text lives in exactly one canonical
        location (LICENSE_NOTICES); this method only formats it.
        """
        notices = active_license_notices(self.env)
        self._log("")
        self._log("=== Open Source License Attribution ===")
        self._log("This application invokes the following third-party tools.")
        self._log("Source code of these tools is NOT bundled; licenses flow")
        self._log("through from upstream. See About > Licenses for full text.")
        self._log("")
        for n in notices:
            self._log(f"  • {n.name} — {n.spdx}")
            self._log(f"      {n.home_url}")
        self._log("")
        self._log("End of license summary.")
        self._log("")

    def _show_license_dialog(self) -> None:
        """Open a modal dialog with the full license text.

        Triggered from the menu / button so the user can review the
        complete attribution text at any time.
        """
        notices = active_license_notices(self.env)
        text = license_banner_full(notices)
        dlg = QMessageBox(self)
        dlg.setWindowTitle("About — Open Source Licenses")
        dlg.setText("This application invokes the following open-source tools:")
        dlg.setInformativeText(text)
        dlg.setStandardButtons(QMessageBox.StandardButton.Ok)
        dlg.exec()

    def _show_pre_transcode_license_summary(self) -> None:
        """One-line license reminder logged at the start of each batch.

        Keeps the legal notice adjacent to the act of transcode, which is
        where redistribution-relevant output is produced.
        """
        notices = active_license_notices(self.env)
        self._log(f"LICENSES: {license_banner_short(notices)}")

    def _disable_unavailable_codecs(self):
        """Grey out AUDIO codec combos whose FFmpeg library is missing.

        Video codecs are NOT disabled here because av1an uses its own
        encoder binaries (svt_av1, vpx, x265) — it does not rely on
        ffmpeg's encoder list for video.

        v3 (OTC-012, SEI CERT STR09-C + MSC04-C): each AudioProfile now
        carries its ffmpeg encoder name as the `ffmpeg_encoder_name`
        field (e.g. "libopus"). We look up that name in env.ffmpeg_libs
        directly. This replaces the v2 approach of indexing into
        `params[1]`, which assumed a fixed params layout and would
        silently break if a profile ever used a different argument order.

        SEI CERT MSC04-C spirit: the source of truth for which library
        each profile needs is the profile itself, not a parallel table.
        """
        libs = self.env.ffmpeg_libs

        for idx, profile in enumerate(AUDIO_PROFILES):
            if idx >= self.audio_combo.count():
                break  # combo not yet populated, defensive

            # v3: use the dedicated field instead of indexing into params.
            lib_name = profile.ffmpeg_encoder_name
            if not lib_name:
                continue  # passthrough profile, no encoder dependency
            if not libs.get(lib_name, False):
                item = self.audio_combo.model().item(idx)
                if item is not None:
                    item.setEnabled(False)
                    item.setToolTip(
                        f"DISABLED: FFmpeg missing {lib_name} encoder. "
                        f"Use Rebuild from Git > ffmpeg + IAMF to enable."
                    )
                # If the currently-selected item is the one we disabled,
                # fall back to the first enabled entry.
                if self.audio_combo.currentIndex() == idx:
                    self.audio_combo.setCurrentIndex(0)

    # ── Process Control ──

    def _parse_extensions(self) -> set[str]:
        raw = self.ext_edit.text()
        exts = set()
        for part in raw.split(","):
            part = part.strip().lower()
            if not part.startswith("."):
                part = "." + part
            if part:
                exts.add(part)
        return exts or DEFAULT_INPUT_EXTENSIONS

    @Slot()
    def _start_process(self):
        in_dir = Path(self.in_path_edit.text())
        out_dir = Path(self.out_path_edit.text())

        if not in_dir.is_dir():
            self._log(f"ERROR: Source directory does not exist: {in_dir}")
            return
        if in_dir == out_dir:
            self._log("ERROR: Source and output directories must be different.")
            return

        # ── Pre-flight: codec/container/audio compatibility check ──
        # Hard incompatibilities (prefixed "INCOMPATIBLE:") block the encode.
        warnings = self._check_combo_compatibility()
        hard_blocks = [w for w in warnings if w.startswith("INCOMPATIBLE")]
        if hard_blocks:
            self._log("ERROR: Aborting — incompatible combination selected.")
            QMessageBox.critical(
                self, "Incompatible Codec Combination",
                "The selected video/audio/container combination cannot be encoded:\n\n"
                + "\n".join(f"• {w.split(':', 1)[1].strip()}" for w in hard_blocks)
                + "\n\nFix the selection and try again."
            )
            return

        # Pre-transcode license reminder — adjacent to the act of transcode
        # so obligations are visible at the moment redistribution-relevant
        # output is produced.
        self._show_pre_transcode_license_summary()

        # If delete is enabled, collect files first for batch confirmation
        if self.del_check.isChecked():
            extensions = self._parse_extensions()
            candidates = [f for f in in_dir.rglob("*") if f.is_file() and f.suffix.lower() in extensions and not f.name.endswith(".scaled_tmp.mkv")]
            if candidates:
                total_size = sum(f.stat().st_size for f in candidates)
                reply = QMessageBox.question(
                    self, "Confirm Batch Delete",
                    f"This will delete {len(candidates)} source file(s) after successful transcode.\n"
                    f"Total size: {total_size / 1_073_741_824:.2f} GB\n\n"
                    f"Proceed?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    self._log("Cancelled: Delete not confirmed.")
                    return

        # ── v4.2.0: av1an is opt-in. Default is ffmpeg-only. ──
        # v4.4.3: flag can come from CLI (--use-av1an) OR UI toggle.
        cli_use_av1an = bool(self.env.av1an_flags.get("use_av1an", False))
        ui_use_av1an = (
            hasattr(self, "av1an_check") and self.av1an_check.isChecked()
        )
        use_av1an = ui_use_av1an or cli_use_av1an

        # ── Pre-flight: av1an VSScript smoke test (main thread — can show dialogs) ──
        use_ffmpeg_fallback = False
        skip_encode = False
        if use_av1an and self.env.av1an_path and self.env.ffmpeg_path:
            self._log("Pre-flight: testing av1an + VapourSynth compatibility...")
            QApplication.processEvents()  # keep UI responsive
            svt_name = self.env.av1an_flags.get("svt_name", "svt_av1")
            ok, detail = _av1an_vsscript_smoke_test(
                self.env.av1an_path,
                self.env.ffmpeg_path,
                self.env.av1an_flags,
                svt_name=svt_name,
            )
            if not ok and "VSScript_API_INCOMPAT" in detail:
                # VSScript ABI mismatch detected — offer rebuild or fallback
                use_ffmpeg_fallback = self._handle_vs_incompat()
                if not use_ffmpeg_fallback:
                    # User chose rebuild or cancel — don't start encoding
                    return
            elif not ok and "INVALID_ENCODER" in detail:
                # Probe mismatch — re-detect the encoder name and retry once.
                self._log(f"  WARN: Encoder name probe mismatch. Re-detecting...")
                QApplication.processEvents()
                new_name = _detect_av1an_svt_encoder(self.env.av1an_path)
                if new_name:
                    self.env.av1an_flags["svt_name"] = new_name
                    self._log(f"  Re-detected SVT-AV1 encoder name: '{new_name}'")
                    # Retry smoke test with corrected name
                    ok2, detail2 = _av1an_vsscript_smoke_test(
                        self.env.av1an_path, self.env.ffmpeg_path,
                        self.env.av1an_flags, svt_name=new_name,
                    )
                    if ok2:
                        self._log("  OK: av1an + VapourSynth working correctly.")
                    else:
                        self._log(f"  FAIL: Still failing after re-detect: {detail2}")
                        return
                else:
                    self._log("  FAIL: Could not determine valid encoder name. Check av1an --help manually.")
                    return
            elif ok:
                self._log("  OK: av1an + VapourSynth working correctly.")
            else:
                # Smoke test failed for an unexpected reason (encoder binary
                # missing, concat method unsupported, av1an panicked, etc.).
                # Previously this was logged as "non-fatal" and the encode
                # proceeded anyway — which produced the "chunks but never
                # saves a file" symptom because every file then failed at
                # the same point. Now we treat unknown smoke failures as
                # hard blocks and offer the user ffmpeg fallback if the
                # selected codec is available, otherwise abort.
                self._log(f"  FAIL: av1an smoke test failed:")
                for line in detail.splitlines()[:12]:
                    self._log(f"    {line}")
                # If ffmpeg has the matching encoder, offer fallback;
                # otherwise abort with an actionable message.
                codec_idx_pre = self.codec_combo.currentIndex()
                if 0 <= codec_idx_pre < len(VIDEO_CODECS):
                    vc = VIDEO_CODECS[codec_idx_pre]
                    lib_key = ffmpeg_lib_key_for(vc.ffmpeg_encoder)  # v3: OTC-007
                    if self.env.ffmpeg_libs.get(lib_key, False):
                        self._log(f"  FFmpeg has {vc.ffmpeg_encoder} — offering fallback.")
                        use_ffmpeg_fallback = self._handle_vs_incompat()
                        if not use_ffmpeg_fallback:
                            return
                    else:
                        self._log(
                            f"  ABORT: ffmpeg also lacks {vc.ffmpeg_encoder}. "
                            f"Install the encoder binary (e.g. SvtAv1EncApp, vpxenc, x265) "
                            f"or use the REBUILD FROM GIT button."
                        )
                        return
                else:
                    self._log("  ABORT: invalid codec selection.")
                    return
        elif not use_av1an:
            # v4.4.3: default path — skip av1an entirely, use ffmpeg.
            self._log("Encode mode: ffmpeg-only (default). Toggle 'av1an (chunk-parallel)' to enable av1an.")
            use_ffmpeg_fallback = True

        codec_idx = self.codec_combo.currentIndex()
        audio_idx = self.audio_combo.currentIndex()
        container_idx = self.container_combo.currentIndex()

        # Safety: clamp codec_idx to valid range
        if not (0 <= codec_idx < len(VIDEO_CODECS)):
            self._log(f"ERROR: Invalid codec index {codec_idx}. Resetting to AV1 (SVT-AV1).")
            codec_idx = 0
            self.codec_combo.blockSignals(True)
            self.codec_combo.setCurrentIndex(0)
            self.codec_combo.blockSignals(False)

        selected_codec = VIDEO_CODECS[codec_idx]
        self._log(f"Codec: {selected_codec.label} (av1an encoder: {selected_codec.av1an_encoder})")

        # v4.6.0: read the ENGINE selector and surface the resolved engine
        # before the queue starts. The worker re-resolves against the same
        # probe data; this line tells the user what is about to happen.
        engine_sel = ("auto", "gpu", "cpu", "hybrid")[self.engine_combo.currentIndex()] \

        if hasattr(self, "engine_combo"):
            # Always write UI state so the worker's env fallback stays in
            # sync when the user flips the combo between runs.
            self.env.av1an_flags["engine"] = engine_sel
        gpu_preview, gpu_api = resolve_gpu_encoder(engine_sel, selected_codec, self.env)
        if gpu_preview:
            self._log(f"Engine: GPU — {gpu_preview} ({gpu_api} hardware encode)")
        elif engine_sel == "gpu":
            gpu_enc = selected_codec.gpu_encoder or "(none for this codec)"
            self._log(f"Engine: GPU requested but {gpu_enc} unavailable — will use CPU.")
        elif engine_sel == "hybrid":
            self._log("Engine: hybrid — GPU lane + CPU lane concurrently (splits the queue by size).")


        # v4.7.0: kwargs shared by every lane of the queue (single
        # worker, or the GPU + CPU lanes in hybrid mode).
        common = dict(
            in_dir=in_dir,
            out_dir=out_dir,
            video_codec=selected_codec,
            audio_profile=AUDIO_PROFILES[audio_idx],
            container=CONTAINER_PROFILES[container_idx],
            crf=self.crf_knob.intValue(),
            preset_label=self.preset_combo.currentText(),
            delete_source=self.del_check.isChecked(),
            extensions=self._parse_extensions(),
            resolution=self._get_current_resolution(),
            audio_level_db=self.vol_knob.value(),
            subtitle_lang=SUBTITLE_OPTIONS[self.subs_combo.currentIndex()][1],
            force=self.force_check.isChecked(),  # v5-01
        )

        # ── v4.7.0: hybrid (GPU + CPU lanes run concurrently) ──
        if engine_sel == "hybrid":
            if self._start_hybrid(common, use_ffmpeg_fallback, use_av1an):
                self.btn_run.setEnabled(False)
                self.btn_run.setText("RUNNING...")
                self.btn_stop.setEnabled(True)
                self.btn_rebuild.setEnabled(False)
                for w in self.workers:
                    w.start()
                return
            self._log("Hybrid unavailable — falling back to a single CPU queue.")
            engine_sel = "cpu"

        self.worker = EncoderWorker(
            **common,
            env=self.env,
            use_ffmpeg_fallback=use_ffmpeg_fallback,
            engine=engine_sel,  # v4.6.0: Auto/GPU/CPU engine selector
        )
        self._set_workers([self.worker])

        self.btn_run.setEnabled(False)
        self.btn_run.setText("RUNNING...")
        self.btn_stop.setEnabled(True)
        self.btn_rebuild.setEnabled(False)
        self.worker.start()

    @Slot()
    def _on_engine_changed(self, index: int):
        """v4.8.1: ENGINE = CPU disables the GPU dropdown (the choice
        would have no effect); every other engine keeps it live."""
        self._update_gpu_combo_state()

    def _update_gpu_combo_state(self):
        cpu_only = self.engine_combo.currentIndex() == 2  # "Engine: CPU"
        self.gpu_combo.setEnabled(not cpu_only)
        self.gpu_combo.setToolTip(
            "ENGINE is CPU — the GPU choice has no effect."
            if cpu_only else
            "Hardware encoder capability class.\n"
            "Auto-detect matches your card via nvidia-smi/lspci and the\n"
            "live encode probe decides what actually works. Forcing a\n"
            "profile also extends the REBUILD FROM GIT dependency tree\n"
            "with that GPU's packages (nv-codec-headers / VAAPI / QSV).\n"
            "'None (CPU-only encode)' opts out even when a GPU exists."
        )

    @Slot()
    def _on_gpu_profile_changed(self, index: int):
        """v4.8.0: persist the GPU capability-profile selection so both
        the engine resolution and the rebuild dep tree pick it up."""
        if not getattr(self, "env", None):
            return  # UI build phase — env probe hasn't run yet
        if index <= 0:
            self.env.av1an_flags["gpu_profile"] = "auto"
            return
        self.env.av1an_flags["gpu_profile"] = GPU_PROFILES[index - 1].key
        self._log(f"GPU profile: {GPU_PROFILES[index - 1].key}")

    def _set_workers(self, workers: list) -> None:
        """v4.7.0: register the active lane worker(s) and wire their
        signals. STOP iterates every lane; _on_finished aggregates the
        per-lane summaries into one."""
        self.workers = list(workers)
        self._hybrid_pending = len(self.workers)
        self._hybrid_totals = [0, 0]
        for w in self.workers:
            w.log_msg.connect(self._log)
            w.progress_msg.connect(self._on_progress)
            w.finished_queue.connect(self._on_finished)

    def _start_hybrid(self, common: dict, use_ffmpeg_fallback: bool,
                      use_av1an: bool) -> bool:
        """v4.7.0: split the queue between a GPU lane (NVENC) and a CPU
        lane (software encoders, or av1an chunk-parallel when opted in —
        so NVENC + chunk workers + software can all run at once).

        Constructs both workers on success and returns True. Returns
        False (with a logged reason) when hybrid cannot apply; the caller
        falls back to a single-lane queue.
        """
        import copy as _copy
        import dataclasses as _dc

        gpu_enc, _gpu_api = resolve_gpu_encoder("gpu", common["video_codec"], self.env)
        if not gpu_enc:
            self._log("Hybrid: no functional NVENC encoder for this codec family.")
            return False

        files = scan_input_files(common["in_dir"], common["extensions"])
        if len(files) < 2:
            self._log("Hybrid: fewer than 2 encodable files — one lane is faster than scheduling.")
            return False

        sizes: dict = {}
        for f in files:
            try:
                sizes[f] = f.stat().st_size
            except OSError:
                pass
        plan = plan_hybrid(
            files, gpu_enc, True, self.env.cpu.logical_threads, sizes=sizes,
        )
        if plan is None or not plan.gpu_files or not plan.cpu_files:
            self._log("Hybrid: size split degenerated to one lane — single lane is faster.")
            return False

        self._log(
            f"HYBRID: GPU lane = {len(plan.gpu_files)} file(s) via {gpu_enc} | "
            f"CPU lane = {len(plan.cpu_files)} file(s) "
            f"(budget {plan.cpu_budget_threads} threads"
            f"{', av1an chunk-parallel' if use_av1an else ''})"
        )
        self._log(
            "  Note: lanes use different encoders — GPU-lane files get NVENC "
            "quality, CPU-lane files get software-encoder quality."
        )

        # CPU lane budget: hold back threads for the GPU lane's
        # decode/scale/mux processes, then let the lane self-budget its
        # av1an workers / ffmpeg threads from the reduced topology.
        cpu_env = _copy.copy(self.env)
        cpu_env.cpu = _dc.replace(
            self.env.cpu, logical_threads=plan.cpu_budget_threads
        )

        gpu_worker = EncoderWorker(
            **common,
            env=self.env,
            use_ffmpeg_fallback=True,   # GPU runs via single-pass ffmpeg
            engine="gpu",
            file_subset=plan.gpu_files,
            lane="gpu",
        )
        cpu_worker = EncoderWorker(
            **common,
            env=cpu_env,
            use_ffmpeg_fallback=use_ffmpeg_fallback,
            engine="cpu",
            file_subset=plan.cpu_files,
            lane="cpu",
            ffmpeg_threads=plan.cpu_budget_threads,
        )
        self.worker = cpu_worker  # primary handle (back-compat)
        self._set_workers([gpu_worker, cpu_worker])
        return True

    def _handle_vs_incompat(self) -> bool:
        """Handle detected VSScript ABI incompatibility.

        Shows a dialog with options:
          1. Rebuild VapourSynth + av1an from git (resolves root cause)
          2. Use ffmpeg fallback (works now, no chunk-parallel)
          3. Cancel

        Returns True if we should use ffmpeg fallback (option 2),
                False if user cancelled or chose to rebuild (rebuild
                starts async and does NOT return here — the user
                will click ENCODE again after it completes).
        """
        self._log("  FAIL: av1an cannot initialize VSScript API.")
        self._log("  The av1an binary was compiled against a different VapourSynth version.")

        # Check ffmpeg fallback availability
        codec_idx = self.codec_combo.currentIndex()
        video_codec = VIDEO_CODECS[codec_idx]
        ffmpeg_enc = video_codec.ffmpeg_encoder
        ffmpeg_lib_key = ffmpeg_lib_key_for(ffmpeg_enc)  # v3: OTC-007
        fallback_possible = self.env.ffmpeg_libs.get(ffmpeg_lib_key, False)

        if fallback_possible:
            btn_rebuild = QPushButton("  Rebuild from Git  ")
            btn_rebuild.setObjectName("btnRebuild")
            btn_fallback = QPushButton("  Use ffmpeg Fallback  ")
            btn_fallback.setObjectName("btnRun")
            btn_cancel = QPushButton("  Cancel  ")
            btn_cancel.setObjectName("btnStop")

            dlg = QMessageBox(self)
            dlg.setWindowTitle("av1an + VapourSynth Version Mismatch")
            dlg.setText(
                "av1an cannot initialize VapourSynth — the installed versions\n"
                "have an ABI incompatibility (common with distro packages).\n\n"
                f"Choose how to proceed:"
            )
            dlg.setInformativeText(
                "• Rebuild from Git — compiles both from source (~10-30 min).\n"
                "  Fixes the root cause. Requires sudo for install.\n"
                f"• ffmpeg Fallback — encode with ffmpeg ({ffmpeg_enc}) now.\n"
                "  No chunk-parallel mode but output quality is identical."
            )
            dlg.addButton(btn_rebuild, QMessageBox.ButtonRole.AcceptRole)
            dlg.addButton(btn_fallback, QMessageBox.ButtonRole.YesRole)
            dlg.addButton(btn_cancel, QMessageBox.ButtonRole.RejectRole)

            dlg.exec()
            clicked = dlg.clickedButton()

            if clicked == btn_rebuild:
                self._log("")
                self._log("User chose: Rebuild VapourSynth + av1an from git.")
                self._start_git_rebuild()
                return False  # don't start encoding — user will retry after build
            elif clicked == btn_fallback:
                self._log("")
                self._log(f"FALLBACK: Switching to pure ffmpeg ({ffmpeg_enc}) encoding.")
                self._log(
                    "  Note: ffmpeg single-pass mode (no chunk-parallel). "
                    "Slower for large files but produces identical output."
                )
                self._log("  Use the REBUILD FROM GIT button to fix av1an for chunk-parallel mode.")
                self._log("")
                return True
            else:
                # Cancel
                self._log("Cancelled by user.")
                return False
        else:
            # No ffmpeg fallback available — offer rebuild or hard cancel
            btn_rebuild = QPushButton("  Rebuild from Git  ")
            btn_rebuild.setObjectName("btnRebuild")
            btn_cancel = QPushButton("  Cancel  ")
            btn_cancel.setObjectName("btnStop")

            dlg = QMessageBox(self)
            dlg.setWindowTitle("av1an + VapourSynth Version Mismatch")
            dlg.setText(
                "av1an cannot initialize VapourSynth — ABI incompatibility.\n\n"
                f"ffmpeg also lacks '{ffmpeg_enc}' — no fallback possible.\n"
                "You must rebuild to proceed."
            )
            dlg.setIcon(QMessageBox.Icon.Critical)
            dlg.addButton(btn_rebuild, QMessageBox.ButtonRole.AcceptRole)
            dlg.addButton(btn_cancel, QMessageBox.ButtonRole.RejectRole)

            dlg.exec()
            clicked = dlg.clickedButton()

            if clicked == btn_rebuild:
                self._log("")
                self._log("User chose: Rebuild VapourSynth + av1an from git (no fallback available).")
                self._start_git_rebuild()
            else:
                self._log("Cancelled by user.")
            return False

    def _start_git_rebuild(self, build_vs: bool = True, build_av1an: bool = True,
                           build_ffmpeg_iamf: bool = False):
        """Start the SourceBuildWorker thread."""
        components = []
        if build_vs: components.append("VapourSynth")
        if build_av1an: components.append("av1an")
        if build_ffmpeg_iamf: components.append("ffmpeg+libiamf")
        self._log(f"Starting source build ({' + '.join(components) if components else 'none'})...")
        self._log("Builds to ~/.local/ and ~/.cargo/bin/ — sudo only if build deps are missing.")
        if build_ffmpeg_iamf:
            self._log("  NOTE: ffmpeg build takes 10-20 min. App must be restarted after.")
        self.btn_run.setEnabled(False)
        self.btn_rebuild.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.status_label.setText("Building from git... (see log)")

        self._build_worker = SourceBuildWorker(
            build_vs=build_vs, build_av1an=build_av1an,
            build_ffmpeg_iamf=build_ffmpeg_iamf,
            gpu_profile_key=self.env.av1an_flags.get("gpu_profile", "auto"),
        )

        self._build_worker.log_msg.connect(self._log)
        self._build_worker.build_done.connect(self._on_build_done)
        self._build_worker.start()

    @Slot(bool, str)
    def _on_build_done(self, success: bool, message: str):
        """Called when SourceBuildWorker finishes."""
        self._log("")
        if success:
            self._log(f"BUILD SUCCESS: {message}")
            self._log("Re-probing environment to pick up new binaries...")
            QApplication.processEvents()

            # Ensure LD_LIBRARY_PATH is set in the main process too.
            #
            # INTENTIONAL os.environ mutation (the ONE kept after the
            # v3-08 refactor). SourceBuildWorker no longer mutates
            # os.environ — it accumulates env changes in its private
            # self._build_env dict and passes that to subprocess.run.
            # But that dict dies with the worker thread. The UI thread
            # must update its OWN os.environ so the next
            # probe_environment() call — which spawns ffmpeg/av1an
            # subprocesses that inherit os.environ — can dlopen the
            # freshly-built VapourSynth / libiamf shared libraries from
            # ~/.local/lib. Without this, the rebuilt binaries would
            # fail to load their dependent libs.
            local_lib = str(Path.home() / ".local" / "lib")
            existing_ld = os.environ.get("LD_LIBRARY_PATH", "")
            if local_lib not in existing_ld:
                os.environ["LD_LIBRARY_PATH"] = f"{local_lib}:{existing_ld}".rstrip(":")

            # Re-probe environment with fresh data
            self.env = probe_environment()

            # Run smoke test again to verify the fix
            if self.env.av1an_path and self.env.ffmpeg_path:
                svt_name = self.env.av1an_flags.get("svt_name", "svt_av1")
                ok, detail = _av1an_vsscript_smoke_test(
                    self.env.av1an_path,
                    self.env.ffmpeg_path,
                    self.env.av1an_flags,
                    svt_name=svt_name,
                )
                if ok:
                    self._log("VERIFIED: av1an + VapourSynth now working correctly!")
                    self._log("Click START PROCESSING to encode.")
                elif "INVALID_ENCODER" in detail:
                    # Re-probe encoder name with the fresh binary
                    self._log("  Re-detecting encoder name from fresh build...")
                    new_name = _detect_av1an_svt_encoder(self.env.av1an_path)
                    if new_name and new_name != svt_name:
                        self.env.av1an_flags["svt_name"] = new_name
                        self._log(f"  Corrected encoder name: '{svt_name}' -> '{new_name}'")
                        ok2, detail2 = _av1an_vsscript_smoke_test(
                            self.env.av1an_path, self.env.ffmpeg_path,
                            self.env.av1an_flags, svt_name=new_name,
                        )
                        if ok2:
                            self._log("VERIFIED: av1an + VapourSynth now working correctly!")
                            self._log("Click START PROCESSING to encode.")
                        else:
                            self._log(f"WARNING: Smoke test still fails: {detail2}")
                    else:
                        self._log(f"WARNING: Could not auto-fix encoder name. Smoke test: {detail}")
                else:
                    self._log(f"WARNING: Build completed but smoke test still fails: {detail}")
                    self._log("You may need to log out/in or restart the app for library changes to take effect.")

            # Update status bar
            distro = self.env.distro
            cpu = self.env.cpu
            vs_info = f" | VS{self.env.vs_version}" if self.env.vs_version else ""
            fb_encs = []
            for vc in VIDEO_CODECS:
                lib_key = ffmpeg_lib_key_for(vc.ffmpeg_encoder)  # v3: OTC-007
                if self.env.ffmpeg_libs.get(lib_key, False):
                    fb_encs.append(vc.ffmpeg_encoder)
            fb_info = f" | ffmpeg-fb:{'+'.join(fb_encs)}" if fb_encs else ""
            self.status_label.setText(
                f"{distro.name} | {cpu.physical_cores}C/{cpu.logical_threads}T | "
                f"av1an v{self.env.av1an_version or '?'} | ffmpeg v{self.env.ffmpeg_version or '?'}{vs_info}{fb_info}"
            )
        else:
            self._log(f"BUILD FAILED: {message}")
            self._log("Try running the build manually in a terminal, or use ffmpeg fallback.")
            self.status_label.setText("Build failed — check log")

        self.btn_run.setEnabled(True)
        self.btn_rebuild.setEnabled(True)

    @Slot()
    def _manual_rebuild(self):
        """Handle the REBUILD FROM GIT button click (manual trigger)."""
        btn_vs_av1an = QPushButton("  VapourSynth + av1an  ")
        btn_vs_av1an.setObjectName("btnRebuild")
        btn_vs_only = QPushButton("  VapourSynth only  ")
        btn_vs_only.setObjectName("btnRebuild")
        btn_av1an_only = QPushButton("  av1an only  ")
        btn_av1an_only.setObjectName("btnRebuild")
        btn_ffmpeg_iamf = QPushButton("  ffmpeg + IAMF  ")
        btn_ffmpeg_iamf.setObjectName("btnRebuild")
        btn_cancel = QPushButton("  Cancel  ")
        btn_cancel.setObjectName("btnStop")

        dlg = QMessageBox(self)
        dlg.setWindowTitle("Rebuild from Git")
        dlg.setText(
            "Select which components to rebuild from git source.\n\n"
            "• VapourSynth — installs to ~/.local (needs sudo for build deps)\n"
            "• av1an — builds via cargo, copies to ~/.cargo/bin (needs sudo for build deps)\n"
            "• ffmpeg + IAMF — builds libiamf + ffmpeg with --enable-libiamf,\n"
            "  installs to ~/.local/bin/ffmpeg (shadows system ffmpeg).\n"
            "  Required to use the IAMF audio codec. ~10-20 min build time.\n\n"
            "Build times: VapourSynth ~2-5 min, av1an ~10-30 min, ffmpeg ~10-20 min"
        )
        dlg.addButton(btn_vs_av1an, QMessageBox.ButtonRole.AcceptRole)
        dlg.addButton(btn_vs_only, QMessageBox.ButtonRole.YesRole)
        dlg.addButton(btn_av1an_only, QMessageBox.ButtonRole.NoRole)
        dlg.addButton(btn_ffmpeg_iamf, QMessageBox.ButtonRole.ActionRole)
        dlg.addButton(btn_cancel, QMessageBox.ButtonRole.RejectRole)

        dlg.exec()
        clicked = dlg.clickedButton()

        if clicked == btn_vs_av1an:
            self._start_git_rebuild(build_vs=True, build_av1an=True)
        elif clicked == btn_vs_only:
            self._start_git_rebuild(build_vs=True, build_av1an=False)
        elif clicked == btn_av1an_only:
            self._start_git_rebuild(build_vs=False, build_av1an=True)
        elif clicked == btn_ffmpeg_iamf:
            self._start_git_rebuild(build_vs=False, build_av1an=False,
                                    build_ffmpeg_iamf=True)

    @Slot(str, int, int)
    def _on_progress(self, filename: str, current: int, total: int):
        self.status_label.setText(f"Processing {current}/{total}: {filename}")

    @Slot(int, int)
    def _on_finished(self, ok: int, fail: int):
        # v4.7.0: hybrid lanes finish independently — aggregate the
        # per-lane summaries and only finalize when the LAST lane exits.
        if len(self.workers) > 1:
            self._hybrid_totals[0] += ok
            self._hybrid_totals[1] += fail
            self._hybrid_pending -= 1
            if self._hybrid_pending > 0:
                self._log(
                    f"Lane done — {ok} ok, {fail} failed. "
                    f"Waiting for the other lane..."
                )
                return
            ok, fail = self._hybrid_totals
            self._hybrid_totals = [0, 0]

        self.btn_run.setEnabled(True)
        self.btn_run.setText("START PROCESSING")
        self.btn_stop.setEnabled(False)
        self.status_label.setText(f"Done — {ok} succeeded, {fail} failed")

        if fail > 0:
            self._log(f"WARNING: {fail} file(s) failed. Check log above for details.")
        if ok > 0:
            self._log(f"All {ok} file(s) archived successfully.")

    @Slot()
    def _stop_process(self):
        if any(w.isRunning() for w in self.workers):
            self._log("STOP: Exiting queue after current file finishes...")
            for w in self.workers:
                w.stop()
            self.btn_stop.setEnabled(False)




# ──────────────────────────────────────────────
#  ENTRY POINT
# ──────────────────────────────────────────────

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = OpenCodecMaster()
    window.show()
    sys.exit(app.exec())