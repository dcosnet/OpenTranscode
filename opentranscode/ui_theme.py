"""MMD3 retro-futuristic media console Qt stylesheet (QSS string).

Brushed aluminum panels, amber/green LED displays, beveled metallic
group boxes, modernized with rounded corners, subtle glow, and
glassmorphism hints. Pure string constant — no imports at all.
"""

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
