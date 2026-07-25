"""OpenCodecMaster (QMainWindow) — the main GUI window.

The top-level window that wires together every other module: codec
profiles (combo boxes), env probe (startup), encoder worker (queue
execution), source builder (rebuild-from-git button), license notices
(About dialog), the MMD3 stylesheet, and the RadioKnob widget.

Also exposes ``launch_gui()``, which is the ``QApplication`` entry
point invoked by ``cli.main()`` and ``python -m opentranscode``.
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QFont, QPalette, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QCheckBox,
    QTextEdit, QFileDialog, QGroupBox, QStatusBar, QMessageBox,
    QStyleFactory,
)

from .codec_profiles import (
    AUDIO_PROFILES,
    CONTAINER_PROFILES,
    DEFAULT_INPUT_EXTENSIONS,
    RESOLUTION_PRESETS,
    SUBTITLE_OPTIONS,
    VIDEO_CODECS,
    AudioProfile,
    ResolutionProfile,
    ffmpeg_lib_key_for,
)
from .encoder_worker import EncoderWorker
from .env_probe import (
    EnvProbe,
    _av1an_vsscript_smoke_test,
    _detect_av1an_svt_encoder,
    probe_environment,
)
from .license_registry import (
    LICENSE_NOTICES,
    active_license_notices,
    license_banner_full,
    license_banner_short,
)
from .source_builder import SourceBuildWorker
from .ui_theme import MMD3_QSS
from .widgets import RadioKnob

class OpenCodecMaster(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("OpenTranscode — dcos.net")
        self.resize(1100, 920)
        self.worker: EncoderWorker | None = None
        self.env: EnvProbe | None = None
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

        # v4.4.3: av1an toggle — UI equivalent of --use-av1an. Default OFF
        # (ffmpeg-only is the reliable default). When ON, the av1an chunk-
        # parallel encode path runs (requires VapourSynth + source plugins).
        # The user-facing label is "av1an (chunk-parallel)" so it's clear
        # what they're opting into without CLI flags.
        self.av1an_check = QCheckBox("av1an (chunk-parallel)")
        self.av1an_check.setToolTip(
            "Use av1an chunk-parallel encoding instead of single-pass ffmpeg.\n"
            "Faster on multi-core machines WITH working VapourSynth setup,\n"
            "but more fragile (y4m pipe breaks, concat failures on phone\n"
            "videos with sparse keyframes). Default OFF = ffmpeg-only,\n"
            "which is more reliable across distros."
        )
        opt_row.addWidget(self.av1an_check)

        # v4.4.4: inline-scale toggle — UI equivalent of --inline-scale.
        # Default OFF (use CRF-16 pre-scale intermediate, robust). When ON,
        # the scale/pad filter chain is passed directly to av1an via
        # --ffmpeg-filter-args, skipping the intermediate file entirely.
        # This eliminates the 0.5-0.8x source size temp file that was
        # crashing 10GB+ encodes with mysterious "ffmpeg error (rc=234)"
        # messages (lossless intermediate was filling the disk).
        self.inline_scale_check = QCheckBox("Inline scale (no intermediate)")
        self.inline_scale_check.setToolTip(
            "Skip the CRF-16 pre-scale intermediate file when a target\n"
            "resolution is selected. The scale/pad filter chain is passed\n"
            "directly to av1an via --ffmpeg-filter-args instead.\n\n"
            "ON  = no intermediate file, faster, no extra disk usage.\n"
            "      May fail on older av1an builds with filter-arg quirks.\n"
            "OFF = pre-scale to a CRF-16 (visually lossless) intermediate\n"
            "      file first, then encode. More robust but writes a\n"
            "      0.5-0.8x source size temp file and runs an extra pass.\n\n"
            "Recommendation: ON for large files (≥10GB) with scaling.\n"
            "Default OFF for compatibility."
        )
        opt_row.addWidget(self.inline_scale_check)
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
            "Compile VapourSynth + av1an from git source.\n"
            "Resolves ABI/version mismatch when package managers\n"
            "install incompatible versions."
        )
        self.btn_rebuild.clicked.connect(self._manual_rebuild)
        self.btn_rebuild.setEnabled(False)
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

        worker_count = max(1, self.env.cpu.physical_cores - 1)
        cpu = self.env.cpu
        self._log(
            f"Chunk-parallel mode: {worker_count} av1an workers "
            f"({cpu.physical_cores} physical cores, {cpu.logical_threads} logical, "
            f"{cpu.threads_per_core}T/core)"
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
        # The av1an chunk-parallel path was too fragile across distros
        # (y4m pipe breaks, SvtAv1EncApp CLI quirks, VapourSynth plugin
        # issues, output buffering making it look hung). ffmpeg's
        # libsvtav1 is invoked as a library, accepts -threads correctly,
        # doesn't need VapourSynth, and produces immediate progress.
        # v4.4.3: the flag can come from the CLI (--use-av1an) OR from
        # the UI toggle (self.av1an_check). UI toggle takes precedence
        # so the user can flip it without restarting.
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
            # v4.2.0: default path — skip av1an entirely, use ffmpeg.
            # This is the reliable path that works on any distro with
            # ffmpeg + libsvtav1/libvpx/libx265 installed. No VapourSynth
            # dependency, no chunk-method selection, no SvtAv1EncApp CLI
            # quirks. Single-pass ffmpeg per file.
            self._log("Encode mode: ffmpeg-only (default). Pass --use-av1an for chunk-parallel.")
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

        # v4.4.4: read the inline-scale checkbox into env.av1an_flags so
        # EncoderWorker.__init__'s fallback path picks it up (matches the
        # pattern used by --use-av1an, --verbose, --skip-existing, etc.).
        # Always write the UI state — this lets the user override a CLI
        # --inline-scale by unchecking the box. The CLI flag pre-checks
        # the box in launch_gui(), so the round-trip is consistent:
        #   CLI --inline-scale → checkbox pre-checked → re-read as True.
        if hasattr(self, "inline_scale_check"):
            self.env.av1an_flags["inline_scale"] = self.inline_scale_check.isChecked()
            if self.inline_scale_check.isChecked():
                self._log("Inline scale: enabled (no intermediate file for scaling).")

        self.worker = EncoderWorker(
            in_dir=in_dir,
            out_dir=out_dir,
            video_codec=selected_codec,
            audio_profile=AUDIO_PROFILES[audio_idx],
            container=CONTAINER_PROFILES[container_idx],
            crf=self.crf_knob.intValue(),
            preset_label=self.preset_combo.currentText(),
            delete_source=self.del_check.isChecked(),
            env=self.env,
            extensions=self._parse_extensions(),
            resolution=self._get_current_resolution(),
            audio_level_db=self.vol_knob.value(),
            use_ffmpeg_fallback=use_ffmpeg_fallback,
            subtitle_lang=SUBTITLE_OPTIONS[self.subs_combo.currentIndex()][1],
            force=self.force_check.isChecked(),  # v5-01
        )
        self.worker.log_msg.connect(self._log)
        self.worker.progress_msg.connect(self._on_progress)
        self.worker.finished_queue.connect(self._on_finished)

        self.btn_run.setEnabled(False)
        self.btn_run.setText("RUNNING...")
        self.btn_stop.setEnabled(True)
        self.btn_rebuild.setEnabled(False)
        self.worker.start()

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
        if self.worker and self.worker.isRunning():
            self._log("STOP: Exiting queue after current file finishes...")
            self.worker.stop()
            self.btn_stop.setEnabled(False)




# ──────────────────────────────────────────────
#  GUI ENTRY POINT
# ──────────────────────────────────────────────

def launch_gui(argv: list[str] | None = None, force: bool = False,
               chunk_method: str | None = None,
               max_workers: int | None = None,
               threads_per_worker: int | None = None,
               use_av1an: bool = False,
               verbose: bool = False,
               skip_existing: bool = True,
               timeout: int = 86400,
               inline_scale: bool = False) -> int:
    """Create the QApplication, show the OpenCodecMaster window, run the Qt event loop.

    This is the GUI entry point invoked by ``cli.main()`` when no
    ``--version`` / ``--dry-run`` / ``--verify-only`` flag is given,
    and by ``python -m opentranscode``. Returns the Qt event-loop
    exit code (0 on clean shutdown).

    v5-01: *force* pre-checks the "Force (skip validation)" checkbox
    in the UI. This is a convenience for users who want to skip ffprobe
    validation on launch (e.g. for the rare edge case where ffprobe
    fails but the file is actually valid). The checkbox can still be
    toggled manually in the UI.

    v4.0.0: *chunk_method* overrides av1an's chunk-method selection.
    When not None, the value is written to ``env.av1an_flags[
    "chunk_method_override"]`` after the environment probe runs.
    "auto" clears any override the probe set; other values ("select",
    "hybrid", "ffms2", etc.) force that method. Useful for forcing
    "select" to avoid the Hybrid chunk method's failure on phone-
    recorded MP4s with sparse keyframes (the "works up until near the
    end, never saves chunks into a full file" bug).

    v4.1.0: *max_workers* / *threads_per_worker* override the
    intelligent worker-count computation in EncoderWorker. When None,
    EncoderWorker derives them from CPU topology so
    ``worker_count * threads_per_worker <= logical_threads - 1``
    (preventing the thread-oversubscription hard-lock on high-core-
    count machines). When set, the values are stored on
    ``env.av1an_flags`` and picked up by EncoderWorker.__init__'s
    fallback path — no ui_window.py code changes needed beyond this
    launch_gui signature.

    v4.2.0: *use_av1an* opts INTO the av1an chunk-parallel path. The
    default is False (ffmpeg-only), which is more reliable across
    distros. av1an was too fragile: y4m pipe breaks on phone-recorded
    MP4s, SvtAv1EncApp CLI rejects --threads, VapourSynth plugin
    issues, output buffering making it look hung. ffmpeg's libsvtav1
    is invoked as a library, doesn't need VapourSynth, and produces
    immediate progress output. Pass use_av1an=True only if you have
    a known-good av1an+VapourSynth setup and want chunk-parallel.

    Equivalent to the v3 ``if __name__ == "__main__":`` block.
    """
    app = QApplication(sys.argv if argv is None else argv)
    window = OpenCodecMaster()
    if force and hasattr(window, "force_check"):
        window.force_check.setChecked(True)
    # v4.0.0: apply --chunk-method override AFTER the window's env probe
    # has run (in __init__). The override is written to env.av1an_flags
    # so every EncoderWorker spawned from this point picks it up.
    if chunk_method is not None and hasattr(window, "env"):
        if chunk_method == "auto":
            window.env.av1an_flags.pop("chunk_method_override", None)
            window._log(f"CLI override: chunk method = auto (cleared probe setting)")
        else:
            window.env.av1an_flags["chunk_method_override"] = chunk_method
            window._log(f"CLI override: chunk method = {chunk_method}")
    # v4.1.0: store --max-workers / --threads-per-worker on env so
    # EncoderWorker.__init__ picks them up via its fallback path. The
    # default (None on both) lets EncoderWorker auto-compute from CPU
    # topology.
    if hasattr(window, "env"):
        if max_workers is not None:
            window.env.av1an_flags["max_workers"] = int(max_workers)
            window._log(f"CLI override: max_workers = {max_workers}")
        if threads_per_worker is not None:
            window.env.av1an_flags["threads_per_worker"] = int(threads_per_worker)
            window._log(f"CLI override: threads_per_worker = {threads_per_worker}")
        # v4.2.0: store --use-av1an flag. The default is False
        # (ffmpeg-only). When True, the av1an pre-flight + smoke test
        # runs as before. When False (default), the smoke test is
        # skipped and use_ffmpeg_fallback is set to True directly,
        # short-circuiting the entire av1an code path.
        window.env.av1an_flags["use_av1an"] = bool(use_av1an)
        if not use_av1an:
            window._log("Encode mode: ffmpeg-only (default). Use --use-av1an for chunk-parallel.")
        else:
            window._log("Encode mode: av1an chunk-parallel (opt-in via --use-av1an).")
        # v4.2.1: store --verbose flag. Default False = quiet log
        # (per-file success/fail + final summary). True = full tech
        # detail (CMD:, live tail, DIAGNOSIS blocks, etc.).
        window.env.av1an_flags["verbose"] = bool(verbose)
        if verbose:
            window._log("Verbose log: enabled (CMD:, live tail, DIAGNOSIS, etc.).")
        # v4.3.0: store --skip-existing flag. Default True = skip files
        # whose output already exists with a matching codec. --force-reencode
        # sets this to False.
        window.env.av1an_flags["skip_existing"] = bool(skip_existing)
        if skip_existing:
            window._log("Skip-existing: enabled (use --force-reencode to disable).")
        else:
            window._log("Skip-existing: disabled (re-encoding all files).")
        # v4.4.0: store --timeout flag. Default 86400s = 24h.
        window.env.av1an_flags["encode_timeout"] = int(timeout)
        if timeout != 86400:
            window._log(f"Per-file timeout: {timeout}s")
        # v4.4.4: store --inline-scale flag. Default False (use CRF-16
        # pre-scale intermediate, robust). When True, the scale/pad filter
        # chain is passed directly to av1an via --ffmpeg-filter-args,
        # skipping the intermediate file entirely. Pre-checks the UI
        # checkbox so the user sees the state. The user can still toggle
        # the checkbox manually — the UI value is re-read at encode time
        # in _start_process.
        window.env.av1an_flags["inline_scale"] = bool(inline_scale)
        if inline_scale and hasattr(window, "inline_scale_check"):
            window.inline_scale_check.setChecked(True)
            window._log("Inline scale: enabled via --inline-scale (no intermediate for scaling).")
    window.show()
    return sys.exit(app.exec())
