"""RadioKnob widget — retro radio-style rotary knob.

A self-contained PySide6 widget (arc range, tick marks, glowing
indicator dot). Has no internal package dependencies — only PySide6
and ``math`` from the stdlib — so it can be imported standalone.
"""

import math

from PySide6.QtCore import Qt, Signal, QPointF, QRectF
from PySide6.QtGui import (
    QFont, QColor, QPainter, QPen, QBrush,
    QRadialGradient, QFontMetrics,
)
from PySide6.QtWidgets import QWidget

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

