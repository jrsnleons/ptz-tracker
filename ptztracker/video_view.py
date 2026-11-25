"""VideoView: paints the video with its overlays and turns mouse input into clicks, drags, zoom and stage edits."""
import logging
import time
from dataclasses import asdict

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QWidget,
)

from .stage import angle_to_pixel, pixel_to_angle
from .tracking import TrackSettings
from .widgets import AMBER, GREEN, INK, INK3, UI_FONT

log = logging.getLogger("ptztracker")


class VideoView(QWidget):
    clicked = Signal(float, float)  # frame coordinates
    rect_drawn = Signal(float, float, float, float)  # frame coordinates (stage drawing)
    drag_move = Signal(float, float)  # joystick drag on the video: pan, tilt in -1..1
    drag_end = Signal()
    wheel_zoom = Signal(int)  # +1 zoom in, -1 zoom out, 0 stop
    bounds_edited = Signal(dict)  # stage bounds changed by dragging their edges (live)
    bounds_edit_done = Signal()

    def __init__(self, settings: TrackSettings):
        super().__init__()
        self.settings = settings
        self.data = None
        self.image = None
        self.draw_mode = False
        self._drag = None  # (start, current) in frame coordinates
        self.edit_mode = False  # drag the stage edges (or the whole box) instead of picking heads
        self._edit = None  # (grab, start bounds, start angle) while an edge is being dragged
        self.setMouseTracking(True)
        self._press = None  # widget position of a press that may become a camera drag
        self._joy = False  # True once the press has turned into a camera drag
        self._wheel_timer = QTimer(self, singleShot=True, interval=250, timeout=lambda: self.wheel_zoom.emit(0))
        self.tracking = False  # green frame round the video while tracking
        self.placeholder = ""  # text shown when there is no video
        self._toast = ("", 0.0)  # (text, time it was shown)
        self._toast_timer = QTimer(self, interval=40, timeout=self._toast_tick)
        self.setMinimumSize(640, 360)
        self.setCursor(Qt.CrossCursor)

    TOAST_SECONDS, TOAST_FADE = 1.2, 0.3

    def toast(self, text):
        """A short message over the video that fades out (e.g. 'Speed 2 · Slow')."""
        self._toast = (text, time.monotonic())
        self._toast_timer.start()
        self.update()

    def _toast_alpha(self):
        text, t0 = self._toast
        if not text:
            return 0.0
        return min(1.0, max(0.0, (self.TOAST_SECONDS - (time.monotonic() - t0)) / self.TOAST_FADE))

    def _toast_tick(self):
        if self._toast_alpha() <= 0:
            self._toast = ("", 0.0)
            self._toast_timer.stop()
        self.update()

    def _paint_toast(self, p):
        a = self._toast_alpha()
        if a <= 0:
            return
        p.setFont(QFont(UI_FONT, 18, QFont.Bold))
        tr = p.fontMetrics().boundingRect(self._toast[0]).adjusted(-22, -12, 22, 12)
        tr.moveCenter(QPointF(self.width() / 2, self.height() / 2).toPoint())
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, int(190 * a)))
        p.drawRoundedRect(QRectF(tr), 12, 12)
        p.setPen(QColor(255, 255, 255, int(255 * a)))
        p.drawText(tr, Qt.AlignCenter, self._toast[0])
        p.setBrush(Qt.NoBrush)

    def show_data(self, data):
        self.data = data
        f = data["frame"]
        h, w = f.shape[:2]
        self.image = QImage(f.data, w, h, f.strides[0], QImage.Format_BGR888).copy()
        self.update()

    def _rect(self):
        if self.image is None:
            return QRectF(self.rect())
        iw, ih = self.image.width(), self.image.height()
        scale = min(self.width() / iw, self.height() / ih)
        dw, dh = iw * scale, ih * scale
        return QRectF((self.width() - dw) / 2, (self.height() - dh) / 2, dw, dh)

    def _to_frame(self, e):
        r = self._rect()
        x = (e.position().x() - r.x()) / r.width() * self.image.width()
        y = (e.position().y() - r.y()) / r.height() * self.image.height()
        return min(max(x, 0), self.image.width()), min(max(y, 0), self.image.height())

    def _stage_geometry(self):
        d = self.data or {}
        pos, bounds = d.get("pos"), d.get("bounds")
        return (pos, bounds) if (pos is not None and bounds is not None) else (None, None)

    def _hit(self, e):
        """Which part of the stage box is under the mouse: a set of 'l','r','t','b' edges, 'move', or None."""
        pos, b = self._stage_geometry()
        if pos is None or self.image is None:
            return None
        iw, ih = self.image.width(), self.image.height()
        r = self._rect()
        tol = 12 * iw / r.width()  # 12 screen pixels, in frame pixels
        x, y = self._to_frame(e)
        mid_p, mid_t = (b.pan_min + b.pan_max) / 2, (b.tilt_min + b.tilt_max) / 2
        left = angle_to_pixel(b.pan_min, mid_t, pos, iw, ih)
        right = angle_to_pixel(b.pan_max, mid_t, pos, iw, ih)
        top = angle_to_pixel(mid_p, b.tilt_max, pos, iw, ih)
        bottom = angle_to_pixel(mid_p, b.tilt_min, pos, iw, ih)
        if None in (left, right, top, bottom):
            return None
        edges = set()
        within_y = top[1] - tol <= y <= bottom[1] + tol
        within_x = left[0] - tol <= x <= right[0] + tol
        if within_y and abs(x - left[0]) <= tol:
            edges.add("l")
        if within_y and abs(x - right[0]) <= tol:
            edges.add("r")
        if within_x and abs(y - top[1]) <= tol:
            edges.add("t")
        if within_x and abs(y - bottom[1]) <= tol:
            edges.add("b")
        if edges:
            return edges
        if left[0] < x < right[0] and top[1] < y < bottom[1]:
            return "move"
        return None

    def _edit_cursor(self, grab):
        if grab == "move":
            return Qt.SizeAllCursor
        if not grab:
            return Qt.CrossCursor
        if len(grab) == 2:
            return Qt.SizeFDiagCursor if grab in ({"l", "t"}, {"r", "b"}) else Qt.SizeBDiagCursor
        return Qt.SizeHorCursor if grab & {"l", "r"} else Qt.SizeVerCursor

    def _edit_drag(self, e):
        pos, _ = self._stage_geometry()
        if pos is None:
            return
        grab, start, (sp, st) = self._edit
        iw, ih = self.image.width(), self.image.height()
        pan, tilt = pixel_to_angle(*self._to_frame(e), pos, iw, ih)
        new = dict(start)
        gap = 1.0  # keep the box at least this many degrees across
        if grab == "move":
            dp, dt = pan - sp, tilt - st
            new = {"pan_min": start["pan_min"] + dp, "pan_max": start["pan_max"] + dp,
                   "tilt_min": start["tilt_min"] + dt, "tilt_max": start["tilt_max"] + dt}  # fmt: skip
        else:
            if "l" in grab:
                new["pan_min"] = min(pan, start["pan_max"] - gap)
            if "r" in grab:
                new["pan_max"] = max(pan, start["pan_min"] + gap)
            if "t" in grab:
                new["tilt_max"] = max(tilt, start["tilt_min"] + gap)
            if "b" in grab:
                new["tilt_min"] = min(tilt, start["tilt_max"] - gap)
        self.bounds_edited.emit(new)

    def mousePressEvent(self, e):
        if self.image is None:
            return
        pt = self._to_frame(e)
        if self.draw_mode:
            self._drag = (pt, pt)
        elif self.edit_mode and self._hit(e):
            pos, b = self._stage_geometry()
            iw, ih = self.image.width(), self.image.height()
            self._edit = (self._hit(e), asdict(b), pixel_to_angle(*pt, pos, iw, ih))
        else:
            self._press, self._joy = e.position(), False

    def _joystick(self, e):
        """Drag toward where the camera should look; farther from the press point = faster."""
        reach = max(self.width(), self.height()) * 0.2
        d = e.position() - self._press
        return float(np.clip(d.x() / reach, -1, 1)), float(np.clip(-d.y() / reach, -1, 1))

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self._drag = (self._drag[0], self._to_frame(e))
            self.update()
        elif self._edit is not None:
            self._edit_drag(e)
        elif self.edit_mode:
            self.setCursor(self._edit_cursor(self._hit(e)))
        elif self._press is not None:
            d = e.position() - self._press
            if not self._joy and abs(d.x()) + abs(d.y()) > 8:
                self._joy = True
                self.setCursor(Qt.ClosedHandCursor)
            if self._joy:
                self.drag_move.emit(*self._joystick(e))

    def wheelEvent(self, e):
        step = e.angleDelta().y()
        if step:
            self.wheel_zoom.emit(1 if step > 0 else -1)
            self._wheel_timer.start()

    def mouseReleaseEvent(self, e):
        if self._edit is not None:
            self._edit = None
            self.bounds_edit_done.emit()
            return
        if self._press is not None:
            joy, self._press, self._joy = self._joy, None, False
            self.setCursor(Qt.CrossCursor)
            if joy:
                self.drag_end.emit()
            else:
                self.clicked.emit(*self._to_frame(e))
            return
        if self._drag is None:
            return
        (x1, y1), (x2, y2) = self._drag[0], self._to_frame(e)
        self._drag = None
        if abs(x2 - x1) > 20 and abs(y2 - y1) > 20:
            self.draw_mode = False
            self.setCursor(Qt.CrossCursor)
            self.rect_drawn.emit(x1, y1, x2, y2)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#050506"))
        p.drawRoundedRect(QRectF(self.rect()), 8, 8)
        p.setBrush(Qt.NoBrush)
        if self.image is None:
            p.setPen(QColor(INK3))
            p.setFont(QFont(UI_FONT, 14))
            p.drawText(self.rect().adjusted(48, 0, -48, 0), Qt.AlignCenter | Qt.TextWordWrap, self.placeholder)
            self._paint_toast(p)
            return
        r = self._rect()
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.drawImage(r, self.image)
        sx = r.width() / self.image.width()
        sy = r.height() / self.image.height()

        def box_rect(b):
            return QRectF(r.x() + b[0] * sx, r.y() + b[1] * sy, (b[2] - b[0]) * sx, (b[3] - b[1]) * sy)

        d = self.data
        s = self.settings
        if s.ignore_below < 1.0:
            iy = r.y() + s.ignore_below * r.height()
            p.fillRect(QRectF(r.x(), iy, r.width(), r.bottom() - iy), QColor(0, 0, 0, 110))
            p.setPen(QPen(QColor(255, 80, 80, 160), 1, Qt.DashLine))
            p.drawLine(QPointF(r.x(), iy), QPointF(r.right(), iy))
        # stage boundary, re-projected for wherever the camera is pointing now
        pos, bounds = d.get("pos"), d.get("bounds")
        iw, ih = self.image.width(), self.image.height()
        if pos is not None and bounds is not None:
            pts = [QPointF(r.x() + x * sx, r.y() + y * sy) for x, y in bounds.outline(pos, iw, ih)]
            if pts:
                p.setPen(QPen(QColor(255, 200, 0, 220), 2, Qt.SolidLine if self.edit_mode else Qt.DashLine))
                p.drawPolygon(QPolygonF(pts))
            if self.edit_mode:
                mp, mt = (bounds.pan_min + bounds.pan_max) / 2, (bounds.tilt_min + bounds.tilt_max) / 2
                handles = [(bounds.pan_min, bounds.tilt_max), (mp, bounds.tilt_max), (bounds.pan_max, bounds.tilt_max),
                           (bounds.pan_min, mt), (bounds.pan_max, mt),
                           (bounds.pan_min, bounds.tilt_min), (mp, bounds.tilt_min), (bounds.pan_max, bounds.tilt_min)]  # fmt: skip
                p.setBrush(QColor(255, 200, 0))
                p.setPen(QPen(QColor(0, 0, 0), 1))
                for hp, ht in handles:
                    pt = angle_to_pixel(hp, ht, pos, iw, ih)
                    if pt is not None:
                        p.drawRect(QRectF(r.x() + pt[0] * sx - 5, r.y() + pt[1] * sy - 5, 10, 10))
                p.setBrush(Qt.NoBrush)

        for det in d["dets"]:
            off = False
            if pos is not None and bounds is not None:
                a = pixel_to_angle((det[0] + det[2]) / 2, (det[1] + det[3]) / 2, pos, iw, ih)
                off = not bounds.contains(*a)
            p.setPen(QPen(QColor(130, 130, 130) if off else QColor(120, 120, 255), 2))
            p.drawRect(box_rect(det))

        # target point + deadzone
        tx, ty = r.x() + s.target_x * r.width(), r.y() + s.target_y * r.height()
        dzx, dzy = s.deadzone_x * r.width(), s.deadzone_y * r.height()
        p.setPen(QPen(QColor(255, 255, 255, 110), 1, Qt.DashLine))
        p.drawRect(QRectF(tx - dzx, ty - dzy, 2 * dzx, 2 * dzy))
        p.setPen(QPen(QColor(255, 255, 255, 180), 1))
        p.drawLine(QPointF(tx - 10, ty), QPointF(tx + 10, ty))
        p.drawLine(QPointF(tx, ty - 10), QPointF(tx, ty + 10))

        if d["box"] is not None:
            color = QColor(AMBER) if d["lost"] else QColor(GREEN)
            br = box_rect(d["box"])
            p.setPen(QPen(color, 3))
            p.drawRect(br)
            p.drawLine(br.center(), QPointF(tx, ty))
            p.setFont(QFont(UI_FONT, 11, QFont.Bold))
            tag = "Lost" if d["lost"] else "Following"
            tr = p.fontMetrics().boundingRect(tag).adjusted(-6, -3, 6, 3)
            tr.moveBottomLeft(QPointF(br.left() - 1.5, br.top() - 3).toPoint())
            p.setPen(Qt.NoPen)
            p.setBrush(color)
            p.drawRoundedRect(QRectF(tr), 4, 4)
            p.setBrush(Qt.NoBrush)
            p.setPen(QColor("#0B2717") if not d["lost"] else QColor("#2A1A00"))
            p.drawText(tr, Qt.AlignCenter, tag)

        # small chips, top right: what the detector sees and what is held still
        chips = [f"{len(d['dets'])} head{'s' if len(d['dets']) != 1 else ''}"]
        if d["ignored"]:
            chips.append(f"{d['ignored']} in audience")
        chips += [name for name, attr in (("Pan locked", "lock_horizontal"), ("Tilt locked", "lock_vertical"), ("Zoom locked", "lock_zoom")) if getattr(s, attr)]
        p.setFont(QFont(UI_FONT, 11))
        x = r.right() - 10
        for text in reversed(chips):
            cr = p.fontMetrics().boundingRect(text).adjusted(-8, -4, 8, 4)
            cr.moveTopRight(QPointF(x, r.y() + 10).toPoint())
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 160))
            p.drawRoundedRect(QRectF(cr), 4, 4)
            p.setPen(QColor(INK))
            p.drawText(cr, Qt.AlignCenter, text)
            x = cr.left() - 6
        p.setBrush(Qt.NoBrush)

        state = d.get("stage_state")
        banner = None
        if self.draw_mode:
            banner = ("Drag a box around the stage", QColor(40, 110, 200, 230))
        elif self.edit_mode:
            banner = ("Editing stage: drag an edge or corner, or the middle to move it", QColor(40, 110, 200, 230))
        elif state == "homing":
            banner = ("Going back to the stage shot…", QColor(200, 120, 0, 230))
        elif state == "waiting":
            banner = ("At the stage shot, waiting for someone on stage", QColor(200, 120, 0, 230))
        if banner:
            p.setFont(QFont(UI_FONT, 14, QFont.Bold))
            br = p.fontMetrics().boundingRect(banner[0]).adjusted(-16, -9, 16, 9)
            br.moveCenter(QPointF(r.center().x(), r.bottom() - 32).toPoint())
            p.setPen(Qt.NoPen)
            p.setBrush(banner[1])
            p.drawRoundedRect(QRectF(br), 8, 8)
            p.setBrush(Qt.NoBrush)
            p.setPen(QColor(255, 255, 255))
            p.drawText(br, Qt.AlignCenter, banner[0])

        if self._drag is not None:
            (x1, y1), (x2, y2) = self._drag
            p.setPen(QPen(QColor(255, 200, 0), 2))
            p.fillRect(box_rect((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))), QColor(255, 200, 0, 40))
            p.drawRect(box_rect((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))))

        self._paint_toast(p)
        if self.tracking:
            p.setPen(QPen(QColor(GREEN), 3))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5), 8, 8)
