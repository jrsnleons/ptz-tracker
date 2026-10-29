"""PTZ Tracker for macOS — auto head tracking + manual PTZ control in one window."""
import json
import os
import sys
import threading
import time
from dataclasses import asdict, fields

import numpy as np
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPalette, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import ptz as ptzmod
from .detector import HeadDetector, default_model_path
from .sources import HAVE_NDI, CVSource, NDIFinder, NDISource
from .stage import CanonPositionPoller, Homing, StageBounds, StageSettings, angle_to_pixel, pixel_to_angle
from .tracking import CamController, TargetTracker, TrackSettings

CONFIG_DIR = os.path.expanduser("~/Library/Application Support/PTZ Tracker")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
PERF_LOG = os.path.expanduser("~/Library/Logs/PTZ Tracker/perf.log")
STALL_STOP = 0.5  # no new video for this long while tracking -> stop the camera

DEFAULT_CONFIG = {
    "video": "NDI",
    "ndi_name": "",
    "ndi_high_bandwidth": False,
    "rtsp_url": "rtsp://192.168.0.80/stream1",
    "webcam_index": 0,
    "control": "VISCA over IP",
    "camera_ip": "192.168.254.122",  # LWNRA-PTZ_CAM (Canon CR-N300) on the church network
    "visca_port": 52381,
    "visca_raw": False,
    "manual_speed": 0.5,
    "auto_pick": True,
    "tracking": asdict(TrackSettings()),
    "stage": asdict(StageSettings()),
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH) as f:
            saved = json.load(f)
        tracking = {**cfg["tracking"], **saved.pop("tracking", {})}
        stage = {**cfg["stage"], **saved.pop("stage", {})}
        cfg.update(saved)
        cfg["tracking"] = tracking
        cfg["stage"] = stage
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


# --------------------------------------------------------------------------- worker


class TrackerWorker(QThread):
    frame_ready = Signal(object)  # dict with frame + overlay data
    error = Signal(str)
    stage_event = Signal(str)
    tracking_stopped = Signal()

    def __init__(self, detector, settings: TrackSettings, stage: StageSettings):
        super().__init__()
        self.detector = detector
        self.settings = settings
        self.stage = stage
        self.poller = None
        self.stage_state = "tracking"  # tracking | homing (on the way back) | waiting (parked on the stage shot)
        self.homing = None
        self._home_reason = ""
        self._home_request = False
        self._cancel_home = False
        self._was_enabled = False
        self._last_ok = 0.0
        self._outside_since = None
        self._home_since = 0.0
        self._present = 0
        self.tracker = TargetTracker()
        self.controller = CamController(settings)
        self.source = None
        self.ptz = ptzmod.PTZBase()
        self.tracking_enabled = False
        self.auto_pick = True
        self.manual_active = False
        self._click = None
        self._release = False
        self._lock = threading.Lock()
        self._running = True
        self._was_sending = False
        self._stalled = False
        self._resume_fov = None  # horizontal field of view to ease to (degrees) while tracking resumes
        self.show_bounds = False  # draw the stage outline even while the stage feature is off (editing)
        self.gui_busy = False  # set while the GUI still has a frame to paint; newer ones are dropped
        self._perf = None
        try:
            os.makedirs(os.path.dirname(PERF_LOG), exist_ok=True)
            self._perf = open(PERF_LOG, "a", buffering=1)
        except OSError:
            pass

    def set_io(self, source, ptz, poller=None):
        with self._lock:
            self.source = source
            self.ptz = ptz or ptzmod.PTZBase()
            self.poller = poller
            self.tracker.release()
            self.controller.reset()

    def click(self, x, y):
        with self._lock:
            self._click = (x, y)

    def release_target(self):
        with self._lock:
            self._release = True

    def request_home(self):
        with self._lock:
            self._home_request = True

    def cancel_home(self):
        with self._lock:
            self._cancel_home = True

    def stop(self):
        self._running = False
        self.wait(2000)

    def go_home(self, cam, reason):
        self._resume_fov = None
        self.tracker.release()
        self.controller.reset()
        slow = self.stage.home_speed < 0.99 and self.stage.home is not None and self.poller is not None
        self.homing = Homing(
            cam, self.poller, self.stage.home_preset, self.stage.home, drive_only=slow, max_speed=self.stage.home_speed
        )
        self._home_reason = reason
        self.stage_state = "homing"
        self._present = 0
        self.stage_event.emit(f"{reason} — going back to the stage shot")

    def _homing_done(self, now):
        result, self.homing = self.homing.result, None
        self.stage_state = "waiting"
        self._home_since = now
        how = "" if result == "preset" else f" ({result})"
        if result == "gave up":
            msg = f"{self._home_reason} — could not reach the stage shot, check the camera"
        elif not self.tracking_enabled:
            msg = f"At the stage shot{how}"
        elif self.stage.auto_resume:
            msg = f"{self._home_reason} — at the stage shot{how}, waiting for someone on stage"
        else:
            self.tracking_enabled = False
            self.tracking_stopped.emit()
            msg = f"{self._home_reason} — at the stage shot{how}, tracking stopped"
        self.stage_event.emit(msg)

    def _stage_check(self, cam, box, inside_box, now):
        """Returns False once it has sent the camera home."""
        if box is not None and inside_box:
            self._last_ok = now
            self._outside_since = None
            return True
        if box is not None:
            self._outside_since = self._outside_since or now
            if now - self._outside_since > self.stage.leave_seconds:
                self.go_home(cam, "Speaker left the stage")
                return False
            return True
        self._outside_since = None
        if now - self._last_ok > self.stage.lost_seconds:
            self.go_home(cam, "Lost the speaker")
            return False
        return True

    def _resume_zoom(self):
        """Zoom speed toward the resume zoom level, or None once it is reached (or not in use)."""
        if self._resume_fov is None:
            return None
        pos = self.poller.latest() if self.poller else None
        if pos is None or self.manual_active:
            self._resume_fov = None
            return None
        err = self._resume_fov - pos.hfov  # + = need a wider view
        if abs(err) < 1.0:
            self._resume_fov = None
            self.ptz.zoom(0)
            return None
        return (-1 if err > 0 else 1) * min(self.stage.resume_speed, 0.4 if abs(err) > 3 else 0.15)

    def _idle(self, cam, src):
        """No new frame: keep homing going, and stop the camera if the video has stalled."""
        if self.homing is not None and self.homing.step():
            self._homing_done(time.monotonic())
        last = src.latest.t
        if self._was_sending and not self.manual_active and not self._stalled and last and time.monotonic() - last > STALL_STOP:
            self._stalled = True
            self.controller.reset()
            cam.stop()
            self.stage_event.emit("Video stalled — camera stopped until frames come back")

    def _log_perf(self, fps, det_ms, loop_ms, age_ms, dropped):
        if self._perf is None:
            return
        try:
            self._perf.write(
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} fps={fps:.1f} detect_ms={det_ms:.1f} "
                f"loop_ms={loop_ms:.1f} frame_age_ms={age_ms:.1f} gui_dropped={dropped} state={self.stage_state} "
                f"tracking={int(self.tracking_enabled)}\n"
            )
        except (OSError, ValueError):
            pass

    def run(self):
        fps_t, fps_n, fps = time.monotonic(), 0, 0.0
        det_sum = loop_sum = age_sum = 0.0
        dropped = 0
        timing = (0.0, 0.0, 0.0)
        while self._running:
            with self._lock:
                src, cam = self.source, self.ptz
                click, self._click = self._click, None
                release, self._release = self._release, False
                home_req, self._home_request = self._home_request, False
                cancel_home, self._cancel_home = self._cancel_home, False
            if src is None:
                time.sleep(0.05)
                continue
            try:
                frame, arrived = src.read()
            except Exception as e:  # source died
                self.error.emit(f"video error: {e}")
                time.sleep(0.5)
                continue
            if frame is None:
                self._idle(cam, src)
                continue
            self._stalled = False
            h, w = frame.shape[:2]
            now = time.monotonic()
            t0 = time.perf_counter()
            all_dets = self.detector.detect(frame)
            t_det = time.perf_counter() - t0
            centre_y = (all_dets[:, 1] + all_dets[:, 3]) / 2
            dets = all_dets[centre_y <= self.settings.ignore_below * h]

            # stage boundary: work out which heads are on stage, in camera angles
            shown = self.stage.get_bounds()
            bounds = shown if self.stage.enabled else None
            pos = self.poller.at(now - self.stage.video_latency) if (shown and self.poller) else None
            stage_on = pos is not None and bounds is not None

            def on_stage(b):
                pt = pixel_to_angle((b[0] + b[2]) / 2, (b[1] + b[3]) / 2, pos, w, h)
                return bounds.contains(*pt, margin=self.stage.margin)

            on_mask = np.array([on_stage(d) for d in dets], dtype=bool) if stage_on else np.ones(len(dets), bool)
            candidates = dets[on_mask]

            if self.tracking_enabled and not self._was_enabled and self.homing is None:
                self.stage_state = "tracking"
                self._last_ok = now
                self._outside_since = None
            self._was_enabled = self.tracking_enabled

            # trip back to the stage shot (automatic, or the "Go to stage shot" button)
            if home_req:
                self.go_home(cam, "Stage shot")
            if self.homing is not None and (cancel_home or self.manual_active):
                self.homing.cancel()  # STOP pressed or the operator took over
                self.homing = None
                self.stage_state = "tracking"
            if self.homing is not None and self.homing.step():
                self._homing_done(now)

            if release:
                self.tracker.release()
            if click is not None:
                if self.tracker.select_at(click[0], click[1], dets):
                    self.stage_state = "tracking"
                    self._last_ok = now
            box = self.tracker.update(dets)

            homing = self.homing is not None
            waiting = self.tracking_enabled and (homing or (stage_on and self.stage_state == "waiting"))
            if waiting and homing:
                self.tracker.release()
                box = None
            elif waiting:
                # parked on the stage shot: resume once someone is clearly on stage again
                self.tracker.release()
                box = None
                settled = self.poller.settled() and now - self._home_since > 1.5
                self._present = self._present + 1 if (settled and len(candidates)) else 0
                if self._present >= 10 and self.tracker.select_largest(candidates):
                    box = self.tracker.box
                    self.stage_state = "tracking"
                    self._last_ok = now
                    waiting = False
                    if self.stage.resume_move and self.stage.resume_shot and self.poller:
                        self._resume_fov = self.stage.resume_shot["hfov"]  # tracking starts now; zoom eases to this
                    self.stage_event.emit("Someone is back on stage — tracking resumed")
            elif not self.tracker.active and self.tracking_enabled and self.auto_pick:
                if self.tracker.select_largest(candidates):
                    box = self.tracker.box

            pan = tilt = zoom = 0.0
            if self.tracking_enabled and not self.manual_active and stage_on and not waiting:
                inside = box is not None and on_stage(box)
                if not self._stage_check(cam, box, inside, now):
                    waiting = True  # just sent the camera home
            if self.tracking_enabled and not self.manual_active and not waiting:
                pan, tilt, zoom = self.controller.compute(box, w, h)
                cur = self.poller.latest() if stage_on else None
                if cur is not None:
                    # never aim the camera past the edge of the stage
                    if (cur.pan >= bounds.pan_max and pan > 0) or (cur.pan <= bounds.pan_min and pan < 0):
                        pan = 0.0
                    if (cur.tilt >= bounds.tilt_max and tilt > 0) or (cur.tilt <= bounds.tilt_min and tilt < 0):
                        tilt = 0.0
                rz = self._resume_zoom()
                if rz is not None:
                    # easing to the resume zoom: keep following, but no faster than the resume speed
                    cap = self.stage.resume_speed
                    pan, tilt, zoom = float(np.clip(pan, -cap, cap)), float(np.clip(tilt, -cap, cap)), rz
                cam.pan_tilt(pan, tilt)
                if rz is not None or not self.settings.lock_zoom:
                    cam.zoom(zoom)
                self._was_sending = True
            elif waiting or self.homing is not None:
                # homing does its own stopping; another stop here would cancel the preset move
                self._was_sending = False
            elif self._was_sending:
                # tracking just turned off: make sure the camera stops
                self.controller.reset()
                if not self.manual_active:
                    cam.stop()
                self._was_sending = False

            fps_n += 1
            det_sum += t_det
            loop_sum += time.perf_counter() - t0
            age_sum += now - arrived
            now = time.monotonic()
            if now - fps_t >= 1.0:
                fps = fps_n / (now - fps_t)
                timing = (det_sum / fps_n * 1000, loop_sum / fps_n * 1000, age_sum / fps_n * 1000)
                self._log_perf(fps, *timing, dropped)
                fps_n, fps_t, det_sum, loop_sum, age_sum, dropped = 0, now, 0.0, 0.0, 0.0, 0
            if self.gui_busy:
                dropped += 1  # the GUI hasn't painted the last frame yet; skip this one
                continue
            self.gui_busy = True
            self.frame_ready.emit(
                {
                    "frame": frame,
                    "dets": dets,
                    "ignored": len(all_dets) - len(dets),
                    "box": None if self.tracker.box is None else self.tracker.box.copy(),
                    "lost": self.tracker.is_lost,
                    "out": (pan, tilt, zoom),
                    "fps": fps,
                    "timing": timing,
                    "pos": pos,
                    "bounds": shown if (self.stage.enabled or self.show_bounds) else None,
                    "off_stage": len(dets) - int(on_mask.sum()),
                    "stage_state": self.stage_state if (self.homing is not None or (stage_on and self.tracking_enabled)) else None,
                }
            )


# --------------------------------------------------------------------------- video view


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
        self.setMinimumSize(640, 360)
        self.setCursor(Qt.CrossCursor)

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
            p.setFont(QFont(".AppleSystemUIFont", 14))
            p.drawText(self.rect(), Qt.AlignCenter, self.placeholder)
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
            p.setFont(QFont(".AppleSystemUIFont", 11, QFont.Bold))
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
        p.setFont(QFont(".AppleSystemUIFont", 11))
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
            p.setFont(QFont(".AppleSystemUIFont", 14, QFont.Bold))
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

        if self.tracking:
            p.setPen(QPen(QColor(GREEN), 3))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5), 8, 8)


# --------------------------------------------------------------------------- look

INK, INK2, INK3 = "#EDEEF0", "#A3A7AD", "#8A8F96"
GROUND, PANEL, RAISED, LINE = "#121314", "#1B1C1E", "#242629", "#303236"
GREEN, RED, AMBER = "#3DD68C", "#F2555A", "#F5A524"

STYLE = f"""
QWidget {{ color: {INK}; font-size: 13px; }}
QMainWindow, #central {{ background: {GROUND}; }}
#topbar {{ background: {PANEL}; border-bottom: 1px solid {LINE}; }}
#rail {{ background: {PANEL}; }}
#railScroll {{ background: {PANEL}; border: 0; border-left: 1px solid {LINE}; }}
QStatusBar {{ background: {PANEL}; border-top: 1px solid {LINE}; color: {INK2}; font-size: 11px; }}
QStatusBar QLabel {{ color: {INK2}; font-size: 11px; padding-right: 12px; }}
QPushButton {{ background: {RAISED}; border: 1px solid {LINE}; border-radius: 8px; padding: 6px 12px; min-height: 18px; }}
QPushButton:hover {{ background: #2C2E32; }}
QPushButton:pressed {{ background: #34373B; }}
QPushButton:disabled {{ color: #5A5D63; }}
QPushButton#track {{ border: 0; border-radius: 10px; padding: 0; min-height: 66px; max-height: 66px; }}
QPushButton#track:checked {{ background: {GREEN}; }}
QCheckBox:disabled, QLabel:disabled {{ color: #5A5D63; }}
QPushButton#stop {{ background: transparent; border: 2px solid {RED}; color: {RED}; border-radius: 10px; font-size: 15px; font-weight: 700; }}
QPushButton#stop:hover {{ background: rgba(242, 85, 90, 0.14); }}
QPushButton#primary {{ background: {INK}; color: {GROUND}; border: 0; border-radius: 10px; font-size: 15px; font-weight: 700; padding: 10px; }}
QPushButton#primary:hover {{ background: #FFFFFF; }}
QPushButton#store {{ background: transparent; border: 1px dashed #5A5D63; color: {INK2}; }}
QPushButton#store:checked {{ background: {AMBER}; border: 1px solid {AMBER}; color: #2A1A00; font-weight: 700; }}
QPushButton#preset {{ font-size: 14px; font-weight: 600; }}
QPushButton#link {{ background: transparent; border: 0; color: {INK2}; padding: 2px 4px; font-size: 12px; }}
QPushButton#link:hover {{ color: {INK}; }}
QPushButton#link:checked {{ color: {AMBER}; font-weight: 600; }}
QPushButton#link:disabled, QPushButton#store:disabled, QLabel#muted:disabled {{ color: #5A5D63; }}
QPushButton#chip {{ font-size: 12px; padding: 4px 12px; }}
QLabel#section {{ color: {INK2}; font-size: 12px; font-weight: 600; }}
QLabel#hint {{ color: {INK3}; font-size: 11px; }}
QLabel#muted {{ color: {INK2}; }}
QLabel#perf {{ color: {INK2}; font-family: Menlo; font-size: 12px; }}
QLabel#cardTitle {{ font-size: 18px; font-weight: 700; }}
QFrame#card {{ background: {PANEL}; border: 1px solid {LINE}; border-radius: 12px; }}
QCheckBox {{ spacing: 8px; }}
QDialog {{ background: {PANEL}; }}
QTabWidget::pane {{ border: 0; }}
QTabBar::tab {{ background: transparent; color: {INK2}; padding: 6px 16px; border-radius: 6px; margin-right: 4px; }}
QTabBar::tab:selected {{ background: {LINE}; color: {INK}; font-weight: 600; }}
"""


def dark_palette():
    pal = QPalette()
    for role, color in (
        (QPalette.Window, PANEL), (QPalette.WindowText, INK), (QPalette.Base, RAISED), (QPalette.AlternateBase, PANEL),
        (QPalette.Text, INK), (QPalette.Button, RAISED), (QPalette.ButtonText, INK), (QPalette.ToolTipBase, RAISED),
        (QPalette.ToolTipText, INK), (QPalette.Highlight, "#4C8BF5"), (QPalette.HighlightedText, "#FFFFFF"),
        (QPalette.PlaceholderText, INK3), (QPalette.Mid, LINE), (QPalette.Dark, GROUND), (QPalette.Light, "#3A3D42"),
    ):  # fmt: skip
        pal.setColor(role, QColor(color))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor("#5A5D63"))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#5A5D63"))
    pal.setColor(QPalette.Disabled, QPalette.WindowText, QColor("#5A5D63"))
    return pal


def resource_path(*parts):
    """Files shipped next to the package: mac/<parts> from source, Contents/Resources/<parts> inside the .app."""
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "..", "Resources", *parts)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", *parts)


def dot_icon(color):
    pm = QPixmap(16, 16)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.NoPen)
    p.drawEllipse(4, 4, 8, 8)
    p.end()
    return QIcon(pm)


# --------------------------------------------------------------------------- helpers


def label(text, name=None, wrap=False):
    lbl = QLabel(text)
    if name:
        lbl.setObjectName(name)
    lbl.setWordWrap(wrap)
    return lbl


def nofocus(b):
    b.setFocusPolicy(Qt.NoFocus)
    return b


def hold_button(text, on_press, on_release, w=40, h=40, tip=None):
    b = nofocus(QPushButton(text))
    b.setFixedSize(w, h)
    b.pressed.connect(on_press)
    b.released.connect(on_release)
    if tip:
        b.setToolTip(tip)
    return b


def slider(minv, maxv, value, on_change, scale=100.0):
    """Slider with its value at the right; the row exposes .slider for resets."""
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    s = QSlider(Qt.Horizontal)
    s.setRange(int(minv * scale), int(maxv * scale))
    s.setValue(int(value * scale))
    s.setFocusPolicy(Qt.NoFocus)
    s.setMinimumWidth(120)
    lbl = label(f"{value:.2f}", "muted")
    lbl.setFixedWidth(36)
    lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def changed(v):
        lbl.setText(f"{v / scale:.2f}")
        on_change(v / scale)

    s.valueChanged.connect(changed)
    lay.addWidget(s)
    lay.addWidget(lbl)
    row.slider, row.scale = s, scale
    return row


def setting(title, help_text, control):
    """A labelled setting: title, the control, then a one-line explanation."""
    box = QWidget()
    v = QVBoxLayout(box)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(3)
    v.addWidget(label(title))
    v.addWidget(control)
    v.addWidget(label(help_text, "hint", wrap=True))
    return box


def section(title, *right):
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(label(title, "section"))
    row.addStretch(1)
    for w in right:
        row.addWidget(w)
    return row


class TrackButton(QPushButton):
    """The big start/stop button: a title, a line under it, and the Space key hint."""

    def __init__(self):
        super().__init__()
        self.setObjectName("track")
        self.setCheckable(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedHeight(68)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 8, 18, 8)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.title = label("")
        self.sub = label("")
        text.addWidget(self.title)
        text.addWidget(self.sub)
        lay.addLayout(text, 1)
        self.key = label("Space")
        lay.addWidget(self.key, 0, Qt.AlignVCenter)
        for w in (self.title, self.sub, self.key):
            w.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.toggled.connect(lambda _: self._paint_text())

    def set_text(self, title, sub):
        self.title.setText(title)
        self.sub.setText(sub)
        self._paint_text()

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == QEvent.EnabledChange:
            self._paint_text()

    def _paint_text(self):
        color = "#5A5D63" if not self.isEnabled() else "#0B2717" if self.isChecked() else INK
        base = f"color: {color}; background: transparent;"
        self.title.setStyleSheet(base + "font-size: 17px; font-weight: 700;")
        self.sub.setStyleSheet(base + "font-size: 12px;")
        self.key.setStyleSheet(base + f"font-size: 11px; font-weight: 600; border: 1px solid {color}; border-radius: 4px; padding: 2px 6px;")


# --------------------------------------------------------------------------- main window

TRACK_SLIDERS = {
    # attr: (title, help, min, max)
    "target_x": ("Face position, across", "Where the head sits left to right in the shot", 0.1, 0.9),
    "target_y": ("Face position, height", "Lower numbers keep more headroom", 0.1, 0.9),
    "target_height": ("Head size", "How big the head is when zoom follows", 0.03, 0.5),
    "ignore_below": ("Ignore heads below", "The audience zone, shaded on the video", 0.3, 1.0),
    "speed": ("Responsiveness", "How hard the camera chases the speaker", 0.1, 3.0),
    "max_speed": ("Top speed", "Caps how fast the camera can move", 0.05, 1.0),
    "deadzone_x": ("Still zone, width", "No pan while the head stays inside the dashed box", 0.0, 0.3),
    "deadzone_y": ("Still zone, height", "No tilt while the head stays inside the dashed box", 0.0, 0.3),
    "exponent": ("Ease near centre", "Higher is gentler for small moves", 1.0, 3.0),
    "smoothing": ("Smoothing", "Higher hides jitter but adds a little lag", 0.05, 1.0),
}
LOCKS = (("Don't pan (left/right)", "lock_horizontal"), ("Don't tilt (up/down)", "lock_vertical"), ("Don't zoom", "lock_zoom"))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PTZ Tracker")
        self.cfg = load_config()
        self.settings = TrackSettings(
            **{k: v for k, v in self.cfg["tracking"].items() if k in {f.name for f in fields(TrackSettings)}}
        )
        self.stage = StageSettings(
            **{k: v for k, v in self.cfg["stage"].items() if k in {f.name for f in fields(StageSettings)}}
        )
        self.finder = NDIFinder() if HAVE_NDI else None
        self.source = None
        self.poller = None
        self.cam = ptzmod.PTZBase()
        self.held = {}  # manual direction keys/buttons currently held -> (pan, tilt)
        self.zoom_held = 0
        self.store_mode = False
        self._perf_at = 0.0

        self.detector = HeadDetector(default_model_path())
        self.worker = TrackerWorker(self.detector, self.settings, self.stage)
        self.worker.auto_pick = self.cfg["auto_pick"]

        self.view = VideoView(self.settings)
        self.view.clicked.connect(self.worker.click)
        self.view.drag_move.connect(lambda pan, tilt: self.manual_press("mouse", pan, tilt))
        self.view.drag_end.connect(lambda: self.manual_release("mouse"))
        self.view.wheel_zoom.connect(self._wheel_zoom)
        self.worker.frame_ready.connect(self._show_frame)
        self.worker.error.connect(self.statusBar().showMessage)
        self.worker.stage_event.connect(self.statusBar().showMessage)
        self.worker.tracking_stopped.connect(lambda: self.set_tracking(False))
        self.view.rect_drawn.connect(self.stage_drawn)
        self.view.bounds_edited.connect(self._bounds_edited)
        self.view.bounds_edit_done.connect(self._bounds_edit_done)

        self.settings_dialog = self._settings_dialog()  # builds the camera options the connect card refers to

        # video with the connect card floating over it until a camera is connected
        video = QWidget()
        vg = QGridLayout(video)
        vg.setContentsMargins(16, 16, 16, 8)
        vg.addWidget(self.view, 0, 0)
        self.connect_card = self._connect_card()
        vg.addWidget(self.connect_card, 0, 0, Qt.AlignCenter)
        tips = label("Click a head to follow it    Drag to move the camera    Scroll to zoom", "hint")
        tips.setContentsMargins(16, 0, 16, 12)
        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(0)
        left.addWidget(video, 1)
        left.addWidget(tips)

        self.rail = self._rail()
        scroll = QScrollArea()
        scroll.setObjectName("railScroll")
        scroll.setWidget(self.rail)
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(360)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addLayout(left, 1)
        body.addWidget(scroll)

        central = QWidget()
        central.setObjectName("central")
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._topbar())
        outer.addLayout(body, 1)
        self.setCentralWidget(central)

        self.stage_status = label("", "muted")
        self.statusBar().addPermanentWidget(self.stage_status)
        self.statusBar().setSizeGripEnabled(False)
        self.resize(1440, 900)

        self._set_connected(False)
        self.set_tracking(False)
        self.status_timer = QTimer(self, interval=1000, timeout=self._refresh_status)
        self.status_timer.start()
        if self.finder:
            QTimer.singleShot(1500, self._refresh_ndi)
        self.worker.start()
        self.statusBar().showMessage(f"Detector ready on {self.detector.provider.replace('ExecutionProvider', '')}")
        QApplication.instance().installEventFilter(self)

    def _show_frame(self, data):
        self.view.show_data(data)
        self.view.repaint()
        self.worker.gui_busy = False
        now = time.monotonic()
        if now - self._perf_at > 0.5:
            self._perf_at = now
            det_ms = data.get("timing", (0, 0, 0))[0]
            self.perf.setText(f"{data['fps']:.0f} fps · detect {det_ms:.0f} ms")

    # ---------------- top bar
    def _topbar(self):
        bar = QWidget()
        bar.setObjectName("topbar")
        bar.setFixedHeight(48)
        h = QHBoxLayout(bar)
        h.setContentsMargins(16, 0, 16, 0)
        self.chip = nofocus(QPushButton("Not connected"))
        self.chip.setObjectName("chip")
        menu = QMenu(self.chip)
        menu.addAction("Disconnect", self.disconnect_camera)
        menu.addAction("Camera settings…", lambda: self.open_settings(0))
        self.chip.setMenu(menu)
        h.addWidget(self.chip)
        h.addStretch(1)
        self.perf = label("", "perf")
        h.addWidget(self.perf)
        h.addSpacing(12)
        gear = nofocus(QPushButton("Settings"))
        gear.setToolTip("Settings (⌘,)")
        gear.clicked.connect(lambda: self.open_settings())
        h.addWidget(gear)
        return bar

    def _set_connected(self, on):
        self.connect_card.setVisible(not on)
        self.rail.setEnabled(on)
        self.view.placeholder = "Waiting for video…" if on else ""
        if on:
            ctrl = self.control_kind.currentText()
            where = f" · {ctrl} {self.ip_edit.text().strip()}" if ctrl == "VISCA over IP" else f" · {ctrl}"
            self.chip.setText(f"{self.source.name}{where}")
            self.chip.setIcon(dot_icon(GREEN))
            if QApplication.focusWidget():
                QApplication.focusWidget().clearFocus()  # so the camera keys work straight away
        else:
            self.chip.setText("Not connected")
            self.chip.setIcon(dot_icon("#5A5D63"))
        self.view.update()

    # ---------------- connection
    def _connect_card(self):
        card = QFrame()
        card.setObjectName("card")
        card.setFixedWidth(440)
        v = QVBoxLayout(card)
        v.setContentsMargins(24, 24, 24, 24)
        v.setSpacing(14)
        v.addWidget(label("Connect to the camera", "cardTitle"))
        v.addWidget(label("Pick the video feed and how to steer the camera. This is remembered for next time.", "muted", wrap=True))

        self.video_kind = QComboBox()
        self.video_kind.addItems(["NDI", "RTSP", "Webcam"])
        self.video_kind.setCurrentText(self.cfg["video"])
        self.video_stack = QStackedWidget()
        ndi_row = QWidget()
        nl = QHBoxLayout(ndi_row)
        nl.setContentsMargins(0, 0, 0, 0)
        self.ndi_combo = QComboBox()
        self.ndi_combo.setEditable(True)
        self.ndi_combo.setCurrentText(self.cfg["ndi_name"])
        refresh = nofocus(QPushButton("Refresh"))
        refresh.setToolTip("Look for NDI sources again")
        refresh.clicked.connect(self._refresh_ndi)
        nl.addWidget(self.ndi_combo, 1)
        nl.addWidget(refresh)
        self.rtsp_edit = QLineEdit(self.cfg["rtsp_url"])
        self.webcam_spin = QSpinBox()
        self.webcam_spin.setRange(0, 9)
        self.webcam_spin.setValue(self.cfg["webcam_index"])
        for w in (ndi_row, self.rtsp_edit, self.webcam_spin):
            self.video_stack.addWidget(w)
        self.video_kind.currentIndexChanged.connect(self.video_stack.setCurrentIndex)
        self.video_stack.setCurrentIndex(self.video_kind.currentIndex())
        row = QHBoxLayout()
        row.addWidget(self.video_kind)
        row.addWidget(self.video_stack, 1)
        v.addWidget(label("Video", "section"))
        v.addLayout(row)

        self.control_kind = QComboBox()
        self.control_kind.addItems(["VISCA over IP", "NDI PTZ", "None"])
        self.control_kind.setCurrentText(self.cfg["control"])
        self.ip_edit = QLineEdit(self.cfg["camera_ip"])
        self.ip_edit.setPlaceholderText("Camera IP address")
        self.control_kind.currentTextChanged.connect(lambda t: self.ip_edit.setEnabled(t == "VISCA over IP"))
        self.ip_edit.setEnabled(self.control_kind.currentText() == "VISCA over IP")
        row = QHBoxLayout()
        row.addWidget(self.control_kind)
        row.addWidget(self.ip_edit, 1)
        v.addWidget(label("Camera control", "section"))
        v.addLayout(row)
        v.addWidget(label("Port, raw VISCA and full-quality NDI are in Settings.", "hint"))

        go = nofocus(QPushButton("Connect"))
        go.setObjectName("primary")
        go.clicked.connect(self.connect_camera)
        v.addWidget(go)
        self.card_status = label("", "muted")
        v.addWidget(self.card_status)
        return card

    def _refresh_ndi(self):
        if not self.finder:
            self.card_status.setText("NDI isn't available in this build")
            return
        current = self.ndi_combo.currentText()
        names = self.finder.names()
        self.ndi_combo.clear()
        self.ndi_combo.addItems(names)
        if current:
            self.ndi_combo.setCurrentText(current)
        self.card_status.setText(f"Found {len(names)} NDI source{'s' if len(names) != 1 else ''} on the network")

    def connect_camera(self):
        self.disconnect_camera()
        kind = self.video_kind.currentText()
        try:
            if kind == "NDI":
                if not self.finder:
                    raise RuntimeError("NDI support is not installed")
                self.source = NDISource(self.finder, self.ndi_combo.currentText(), self.ndi_hq.isChecked())
            elif kind == "RTSP":
                self.source = CVSource(self.rtsp_edit.text().strip())
            else:
                self.source = CVSource(self.webcam_spin.value())

            ctrl = self.control_kind.currentText()
            if ctrl == "VISCA over IP":
                self.cam = ptzmod.VISCAoverIP(self.ip_edit.text().strip(), self.port_spin.value(), self.raw_check.isChecked())
            elif ctrl == "NDI PTZ":
                if not isinstance(self.source, NDISource):
                    raise RuntimeError("NDI PTZ needs the video source to be NDI")
                self.cam = ptzmod.NDIPTZ(self.source)
            else:
                self.cam = ptzmod.PTZBase()
        except Exception as e:
            self.disconnect_camera()
            QMessageBox.warning(self, "Couldn't connect", str(e))
            return
        ip = self.ip_edit.text().strip()
        # position readback for the stage boundary (Canon HTTP API, read-only)
        self.poller = CanonPositionPoller(ip) if ip else None
        self.worker.set_io(self.source, self.cam, self.poller)
        self._save()
        self._set_connected(True)
        self.statusBar().showMessage(f"Connected to {self.source.name}")

    def disconnect_camera(self):
        self.set_tracking(False)
        self.worker.set_io(None, None)
        time.sleep(0.05)
        for obj in (self.cam, self.source):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        if self.poller is not None:
            self.poller.close()
            self.poller = None
        self.cam = ptzmod.PTZBase()
        self.source = None
        self.view.image = None
        self.perf.setText("")
        self._set_connected(False)

    def _refresh_status(self):
        video = f"Video: {self.source.name}" if self.source else "Video: none"
        self.conn_status.setText(f"{video}\n{self.cam.status()}")
        pos = self.poller.status() if self.poller else "Camera position: not connected"
        b = self.stage.get_bounds()
        if b is None:
            stage = "No stage drawn yet"
        else:
            stage = f"Stage pan {b.pan_min:+.1f}° to {b.pan_max:+.1f}°, tilt {b.tilt_min:+.1f}° to {b.tilt_max:+.1f}°"
        self.stage_status.setText(f"{pos}    {stage}")

    # ---------------- control rail
    def _rail(self):
        rail = QWidget()
        rail.setObjectName("rail")
        v = QVBoxLayout(rail)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(22)
        v.addLayout(self._tracking_box())
        v.addLayout(self._stage_box())
        v.addLayout(self._preset_box())
        v.addLayout(self._manual_box())
        v.addStretch(1)
        stop = nofocus(QPushButton("Stop camera    X"))
        stop.setObjectName("stop")
        stop.setMinimumHeight(48)
        stop.setToolTip("Stops tracking and every camera move (X)")
        stop.clicked.connect(self.emergency_stop)
        v.addWidget(stop)
        return rail

    # ---------------- tracking
    def _tracking_box(self):
        v = QVBoxLayout()
        v.setSpacing(10)
        self.track_btn = TrackButton()
        self.track_btn.toggled.connect(self.set_tracking)
        v.addWidget(self.track_btn)
        row = QHBoxLayout()
        self.auto_pick = QCheckBox("Auto-pick biggest head")
        self.auto_pick.setChecked(self.cfg["auto_pick"])
        self.auto_pick.toggled.connect(lambda on: setattr(self.worker, "auto_pick", on))
        rel = nofocus(QPushButton("Release  Esc"))
        rel.setToolTip("Stop following this person so another can be picked (Esc)")
        rel.clicked.connect(self.worker.release_target)
        row.addWidget(self.auto_pick, 1)
        row.addWidget(rel)
        v.addLayout(row)
        return v

    def set_tracking(self, on):
        if self.track_btn.isChecked() != on:
            self.track_btn.setChecked(on)  # re-enters via toggled
            return
        self.worker.tracking_enabled = on
        if on:
            self.track_btn.set_text("Tracking", "Following the speaker · press to stop")
        else:
            self.track_btn.set_text("Start tracking", "Follows the speaker on stage")
        self.view.tracking = on
        self.view.update()

    # ---------------- stage boundary
    def _stage_box(self):
        v = QVBoxLayout()
        v.setSpacing(8)
        draw = nofocus(QPushButton("Draw…"))
        draw.setObjectName("link")
        draw.setToolTip("Zoom out to the wide stage shot, then drag a box around the stage")
        draw.clicked.connect(self.start_stage_draw)
        self.edit_btn = nofocus(QPushButton("Edit edges"))
        self.edit_btn.setObjectName("link")
        self.edit_btn.setCheckable(True)
        self.edit_btn.setToolTip("Drag the stage edges or corners (or the middle to move the whole box) instead of redrawing it")
        self.edit_btn.toggled.connect(self.set_edit_stage)
        v.addLayout(section("Stage", draw, self.edit_btn))
        home = nofocus(QPushButton("Go to stage shot    G"))
        home.setMinimumHeight(40)
        home.setStyleSheet("font-weight: 600;")
        home.setToolTip("Stop tracking and go back to the wide stage shot (G)")
        home.clicked.connect(self.go_stage_home)
        v.addWidget(home)
        self.stage_check = QCheckBox("Return to the stage shot when the speaker leaves")
        self.stage_check.setChecked(self.stage.enabled)
        self.stage_check.toggled.connect(lambda on: setattr(self.stage, "enabled", on))
        v.addWidget(self.stage_check)
        self.resume_check = QCheckBox("Resume tracking when someone is back on stage")
        self.resume_check.setChecked(self.stage.auto_resume)
        self.resume_check.toggled.connect(lambda on: setattr(self.stage, "auto_resume", on))
        v.addWidget(self.resume_check)
        return v

    def start_stage_draw(self):
        pos = self.poller.latest() if self.poller else None
        if pos is None or self.view.image is None:
            QMessageBox.information(
                self, "Draw stage", "Connect to the camera first. The app needs video and the camera's position (Camera IP)."
            )
            return
        self.settings_dialog.hide()
        self.set_tracking(False)
        self.view.draw_mode = True
        self.view.setCursor(Qt.CrossCursor)
        self.statusBar().showMessage("Zoom out to the wide stage shot, then drag a box around the stage")

    def stage_drawn(self, x1, y1, x2, y2):
        pos = self.poller.latest() if self.poller else None
        if pos is None:
            self.statusBar().showMessage("Lost the camera position — stage not saved")
            return
        w, h = self.view.image.width(), self.view.image.height()
        bounds = StageBounds.from_rect(x1, y1, x2, y2, pos, w, h)
        self.stage.bounds = asdict(bounds)
        self.stage.home = {"pan": pos.pan, "tilt": pos.tilt, "hfov": pos.hfov}
        self.stage.enabled = True
        self.stage_check.setChecked(True)
        # the shot the stage was drawn on becomes the "go home" shot
        self.cam.store_preset(self.stage.home_preset)
        self._save()
        self._refresh_status()
        self.statusBar().showMessage(f"Stage saved, and this shot is stored as preset {self.stage.home_preset}")

    def set_edit_stage(self, on):
        if on and not self.stage.bounds:
            self.edit_btn.setChecked(False)
            QMessageBox.information(self, "Edit stage", "Draw the stage once first, then you can adjust its edges here.")
            return
        if on and (self.poller is None or self.poller.latest() is None):
            self.edit_btn.setChecked(False)
            QMessageBox.information(self, "Edit stage", "Connect to the camera first (the stage edges need its position).")
            return
        self.set_tracking(False) if on else None
        self.view.edit_mode = on
        self.worker.show_bounds = on
        self.edit_btn.setText("Done editing" if on else "Edit edges")
        self.view.setCursor(Qt.CrossCursor)
        self.view.update()
        self.statusBar().showMessage("Drag the yellow edges to adjust the stage" if on else "Stage edit finished")

    def _bounds_edited(self, new):
        self.stage.bounds = new

    def _bounds_edit_done(self):
        self._save()
        self.statusBar().showMessage("Stage updated")

    def _resume_move_toggled(self, on):
        if on and not self.stage.resume_shot:
            self.resume_move_check.setChecked(False)
            QMessageBox.information(
                self, "Resume shot", "Zoom to the level you want first, then press 'Set resume zoom from current view'."
            )
            return
        self.stage.resume_move = on

    def set_resume_shot(self):
        pos = self.poller.latest() if self.poller else None
        if pos is None:
            QMessageBox.information(self, "Resume shot", "Connect to the camera first (it needs the camera position).")
            return
        self.stage.resume_shot = {"pan": pos.pan, "tilt": pos.tilt, "hfov": pos.hfov}
        self.resume_move_check.setChecked(True)
        self._save()
        self.statusBar().showMessage(f"Resume zoom saved: view {pos.hfov:.1f}°")

    def go_stage_home(self):
        self.set_tracking(False)
        self.worker.request_home()

    # ---------------- presets
    def _preset_box(self):
        v = QVBoxLayout()
        v.setSpacing(8)
        v.addLayout(section("Presets", label("1–9 recall · ⇧1–9 store", "hint")))
        grid = QGridLayout()
        grid.setSpacing(6)
        for i in range(9):
            b = nofocus(QPushButton(str(i + 1)))
            b.setObjectName("preset")
            b.setMinimumHeight(40)
            b.clicked.connect(lambda _=False, n=i + 1: self.preset(n))
            grid.addWidget(b, i // 5, i % 5)
        self.store_btn = nofocus(QPushButton("Store"))
        self.store_btn.setObjectName("store")
        self.store_btn.setCheckable(True)
        self.store_btn.setMinimumHeight(40)
        self.store_btn.setToolTip("Then press a number to save the current shot there")
        self.store_btn.toggled.connect(self._store_toggled)
        grid.addWidget(self.store_btn, 1, 4)
        v.addLayout(grid)
        return v

    def _store_toggled(self, on):
        self.store_mode = on
        if on:
            self.statusBar().showMessage("Store mode: press a preset number to save the current shot")

    def preset(self, n, store=None):
        store = self.store_mode if store is None else store
        if store:
            self.cam.store_preset(n)
            self.statusBar().showMessage(f"Stored preset {n}")
            self.store_btn.setChecked(False)
        else:
            self.set_tracking(False)
            self.cam.recall_preset(n)
            self.statusBar().showMessage(f"Recalled preset {n}")

    # ---------------- manual control
    def _manual_box(self):
        v = QVBoxLayout()
        v.setSpacing(8)
        v.addLayout(section("Manual", label("Arrow keys or WASD", "hint")))
        top = QHBoxLayout()
        top.setSpacing(14)
        pad = QGridLayout()
        pad.setSpacing(4)
        dirs = {
            (0, 0): ("↖", -1, 1), (0, 1): ("↑", 0, 1), (0, 2): ("↗", 1, 1),
            (1, 0): ("←", -1, 0), (1, 2): ("→", 1, 0),
            (2, 0): ("↙", -1, -1), (2, 1): ("↓", 0, -1), (2, 2): ("↘", 1, -1),
        }  # fmt: skip
        for (r, c), (txt, px, ty) in dirs.items():
            key = f"btn{r}{c}"
            pad.addWidget(
                hold_button(txt, lambda k=key, px=px, ty=ty: self.manual_press(k, px, ty), lambda k=key: self.manual_release(k)),
                r,
                c,
            )
        home = nofocus(QPushButton("H"))
        home.setFixedSize(40, 40)
        home.setToolTip("Camera home position (H)")
        home.setStyleSheet(f"QPushButton {{ background: {PANEL}; border-radius: 20px; color: {INK2}; }} QPushButton:disabled {{ color: #5A5D63; }}")
        home.clicked.connect(lambda: self.cam.home())
        pad.addWidget(home, 1, 1)
        top.addLayout(pad)

        side = QGridLayout()
        side.setSpacing(6)
        side.addWidget(label("Zoom", "muted"), 0, 0)
        side.addWidget(hold_button("−   Q", lambda: self.zoom_press(-1), self.zoom_release, 64, 34, "Zoom out (Q or −)"), 0, 1)
        side.addWidget(hold_button("+   E", lambda: self.zoom_press(1), self.zoom_release, 64, 34, "Zoom in (E or +)"), 0, 2)
        side.addWidget(label("Focus", "muted"), 1, 0)
        side.addWidget(hold_button("Near  [", lambda: self.cam.focus(-0.5), lambda: self.cam.focus(0), 64, 34, "Focus nearer ([)"), 1, 1)
        side.addWidget(hold_button("Far  ]", lambda: self.cam.focus(0.5), lambda: self.cam.focus(0), 64, 34, "Focus farther (])"), 1, 2)
        af = nofocus(QPushButton("Auto"))
        af.setToolTip("Turn on continuous auto focus")
        af.clicked.connect(lambda: self.cam.autofocus(True))
        once = nofocus(QPushButton("Once  F"))
        once.setToolTip("Focus once on the current shot, then hold it (F)")
        once.clicked.connect(lambda: self.cam.one_push_af())
        side.addWidget(af, 2, 1)
        side.addWidget(once, 2, 2)
        top.addLayout(side, 1)
        v.addLayout(top)
        row = QHBoxLayout()
        row.addWidget(label("Speed", "muted"))
        row.addWidget(slider(0.05, 1.0, self.cfg["manual_speed"], lambda val: self.cfg.__setitem__("manual_speed", val)), 1)
        v.addLayout(row)
        return v

    def _apply_manual(self):
        sp = self.cfg["manual_speed"]
        pan = float(np.clip(sum(p for p, _ in self.held.values()), -1, 1)) * sp
        tilt = float(np.clip(sum(t for _, t in self.held.values()), -1, 1)) * sp
        self.worker.manual_active = bool(self.held) or self.zoom_held != 0
        self.cam.pan_tilt(pan, tilt)

    def _wheel_zoom(self, direction):
        if direction:
            self.zoom_press(direction)
        else:
            self.zoom_release()

    def manual_press(self, key, pan, tilt):
        self.held[key] = (pan, tilt)
        self._apply_manual()

    def manual_release(self, key):
        self.held.pop(key, None)
        self._apply_manual()

    def zoom_press(self, direction):
        self.zoom_held = direction
        self.worker.manual_active = True
        self.cam.zoom(direction * max(self.cfg["manual_speed"], 0.3))

    def zoom_release(self):
        self.zoom_held = 0
        self.cam.zoom(0)
        self.worker.manual_active = bool(self.held)

    def emergency_stop(self):
        self.worker.cancel_home()
        self.set_tracking(False)
        self.held.clear()
        self.zoom_held = 0
        self.worker.manual_active = False
        self.cam.stop()
        self.statusBar().showMessage("Camera stopped")

    # ---------------- settings sheet
    def _settings_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Settings")
        dlg.setMinimumSize(780, 600)
        v = QVBoxLayout(dlg)
        v.setContentsMargins(20, 16, 20, 16)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._camera_tab(), "Camera")
        self.tabs.addTab(self._tracking_tab(), "Tracking")
        self.tabs.addTab(self._stage_tab(), "Stage")
        v.addWidget(self.tabs, 1)
        done = QPushButton("Done")
        done.setObjectName("primary")
        done.setFixedWidth(120)
        done.clicked.connect(dlg.accept)
        dlg.accepted.connect(self._save)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(done)
        v.addLayout(row)
        return dlg

    def open_settings(self, tab=None):
        if tab is not None:
            self.tabs.setCurrentIndex(tab)
        self.settings_dialog.show()
        self.settings_dialog.raise_()

    def _camera_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(4, 16, 4, 4)
        v.setSpacing(16)
        self.ndi_hq = QCheckBox("Full-quality NDI")
        self.ndi_hq.setChecked(self.cfg["ndi_high_bandwidth"])
        v.addWidget(setting("NDI video", "Off uses the low-bandwidth stream, which tracks just as well with less delay and CPU", self.ndi_hq))
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(self.cfg["visca_port"])
        self.port_spin.setFixedWidth(120)
        v.addWidget(setting("VISCA port", "52381 for most cameras, including the Canon CR-N300", self.port_spin))
        self.raw_check = QCheckBox("Raw VISCA (no IP header)")
        self.raw_check.setChecked(self.cfg["visca_raw"])
        v.addWidget(setting("VISCA format", "Only for cameras that reject the standard VISCA-over-IP header", self.raw_check))
        self.conn_status = label("", "muted", wrap=True)
        v.addWidget(setting("Status", "Changes here apply the next time you connect", self.conn_status))
        v.addStretch(1)
        return w

    def _tracking_tab(self):
        w = QWidget()
        grid = QGridLayout(w)
        grid.setContentsMargins(4, 16, 4, 4)
        grid.setHorizontalSpacing(32)
        grid.setVerticalSpacing(14)
        s = self.settings
        self.track_rows = {}
        for attr, (title, help_text, lo, hi) in TRACK_SLIDERS.items():
            row = slider(lo, hi, getattr(s, attr), lambda val, a=attr: setattr(self.settings, a, val))
            self.track_rows[attr] = row
        left = QVBoxLayout()
        left.setSpacing(12)
        left.addWidget(label("Framing", "section"))
        for attr in ("target_x", "target_y", "target_height", "ignore_below"):
            left.addWidget(setting(TRACK_SLIDERS[attr][0], TRACK_SLIDERS[attr][1], self.track_rows[attr]))
        left.addSpacing(8)
        left.addWidget(label("Hold still", "section"))
        self.lock_checks = {}
        for text, attr in LOCKS:
            c = QCheckBox(text)
            c.setChecked(getattr(s, attr))
            c.toggled.connect(lambda on, a=attr: setattr(self.settings, a, on))
            self.lock_checks[attr] = c
            left.addWidget(c)
        left.addStretch(1)
        right = QVBoxLayout()
        right.setSpacing(12)
        right.addWidget(label("Movement", "section"))
        for attr in ("speed", "max_speed", "deadzone_x", "deadzone_y", "exponent", "smoothing"):
            right.addWidget(setting(TRACK_SLIDERS[attr][0], TRACK_SLIDERS[attr][1], self.track_rows[attr]))
        reset = QPushButton("Reset tracking to defaults")
        reset.setObjectName("link")
        reset.clicked.connect(self._reset_tracking)
        right.addWidget(reset, 0, Qt.AlignLeft)
        right.addStretch(1)
        grid.addLayout(left, 0, 0)
        grid.addLayout(right, 0, 1)
        scroll = QScrollArea()
        scroll.setWidget(w)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        return scroll

    def _reset_tracking(self):
        d = TrackSettings()
        for attr, row in self.track_rows.items():
            row.slider.setValue(int(getattr(d, attr) * row.scale))
        for attr, c in self.lock_checks.items():
            c.setChecked(getattr(d, attr))

    def _stage_tab(self):
        w = QWidget()
        grid = QGridLayout(w)
        grid.setContentsMargins(4, 16, 4, 4)
        grid.setHorizontalSpacing(32)
        grid.setVerticalSpacing(14)
        left = QVBoxLayout()
        left.setSpacing(12)
        left.addWidget(label("Stage shot", "section"))
        spin = QSpinBox()
        spin.setRange(1, 99)
        spin.setValue(self.stage.home_preset)
        spin.setFixedWidth(120)
        spin.valueChanged.connect(lambda n: setattr(self.stage, "home_preset", n))
        left.addWidget(setting("Stage shot preset", "Drawing the stage stores the wide shot in this preset", spin))
        draw = QPushButton("Draw new stage…")
        draw.clicked.connect(self.start_stage_draw)
        left.addWidget(draw, 0, Qt.AlignLeft)
        left.addWidget(setting("Speed back to the stage shot", "How fast the camera returns", slider(0.1, 1.0, self.stage.home_speed, lambda x: setattr(self.stage, "home_speed", x))))
        left.addWidget(setting("Off stage for (seconds)", "How long the speaker can be off stage before the camera goes back", slider(0.5, 10, self.stage.leave_seconds, lambda x: setattr(self.stage, "leave_seconds", x))))
        left.addWidget(setting("Lost for (seconds)", "How long nobody can be seen before the camera goes back", slider(0.5, 10, self.stage.lost_seconds, lambda x: setattr(self.stage, "lost_seconds", x))))
        left.addStretch(1)
        right = QVBoxLayout()
        right.setSpacing(12)
        right.addWidget(label("When someone comes back", "section"))
        self.resume_move_check = QCheckBox("Ease to a set zoom while tracking resumes")
        self.resume_move_check.setChecked(self.stage.resume_move)
        self.resume_move_check.toggled.connect(self._resume_move_toggled)
        right.addWidget(self.resume_move_check)
        set_resume = QPushButton("Use the current zoom")
        set_resume.setToolTip("Zoom to the level you want when someone comes back, then press this. Only the zoom is used; the position follows the person")
        set_resume.clicked.connect(self.set_resume_shot)
        right.addWidget(set_resume, 0, Qt.AlignLeft)
        right.addWidget(setting("Resume zoom speed", "How fast it eases to that zoom", slider(0.1, 1.0, self.stage.resume_speed, lambda x: setattr(self.stage, "resume_speed", x))))
        right.addStretch(1)
        grid.addLayout(left, 0, 0)
        grid.addLayout(right, 0, 1)
        return w

    # ---------------- keyboard
    KEY_DIRS = {
        Qt.Key_Left: (-1, 0), Qt.Key_A: (-1, 0),
        Qt.Key_Right: (1, 0), Qt.Key_D: (1, 0),
        Qt.Key_Up: (0, 1), Qt.Key_W: (0, 1),
        Qt.Key_Down: (0, -1), Qt.Key_S: (0, -1),
    }  # fmt: skip

    def eventFilter(self, obj, e):
        if e.type() not in (QEvent.KeyPress, QEvent.KeyRelease) or not self.isActiveWindow():
            return False
        if isinstance(QApplication.focusWidget(), (QLineEdit, QSpinBox, QComboBox)):
            return False
        press = e.type() == QEvent.KeyPress
        if e.isAutoRepeat():
            return True
        k = e.key()
        if k in self.KEY_DIRS:
            name = f"key{k}"
            if press:
                self.manual_press(name, *self.KEY_DIRS[k])
            else:
                self.manual_release(name)
            return True
        if k in (Qt.Key_Q, Qt.Key_Minus):
            self.zoom_press(-1) if press else self.zoom_release()
            return True
        if k in (Qt.Key_E, Qt.Key_Equal, Qt.Key_Plus):
            self.zoom_press(1) if press else self.zoom_release()
            return True
        if k in (Qt.Key_BracketLeft, Qt.Key_BracketRight):
            self.cam.focus((-0.5 if k == Qt.Key_BracketLeft else 0.5) if press else 0)
            return True
        if not press:
            return False
        if k == Qt.Key_Comma and e.modifiers() & Qt.ControlModifier:  # ⌘, on a Mac
            self.open_settings()
            return True
        if k == Qt.Key_X:
            self.emergency_stop()
            return True
        if k == Qt.Key_G:
            self.go_stage_home()
            return True
        if k == Qt.Key_F:
            self.cam.one_push_af()
            return True
        if k == Qt.Key_Space:
            self.set_tracking(not self.track_btn.isChecked())
            return True
        if k == Qt.Key_Escape:
            self.worker.release_target()
            return True
        if k == Qt.Key_H:
            self.cam.home()
            return True
        # Shift+digit arrives as a symbol on most layouts, so use the native scan text too
        digits = {Qt.Key_1 + i: i + 1 for i in range(9)}
        shifted = {Qt.Key_Exclam: 1, Qt.Key_At: 2, Qt.Key_NumberSign: 3, Qt.Key_Dollar: 4, Qt.Key_Percent: 5,
                   Qt.Key_AsciiCircum: 6, Qt.Key_Ampersand: 7, Qt.Key_Asterisk: 8, Qt.Key_ParenLeft: 9}  # fmt: skip
        if k in digits:
            self.preset(digits[k], store=bool(e.modifiers() & Qt.ShiftModifier) or None)
            return True
        if k in shifted:
            self.preset(shifted[k], store=True)
            return True
        return False

    # ---------------- lifecycle
    def _save(self):
        self.cfg.update(
            video=self.video_kind.currentText(),
            ndi_name=self.ndi_combo.currentText(),
            ndi_high_bandwidth=self.ndi_hq.isChecked(),
            rtsp_url=self.rtsp_edit.text(),
            webcam_index=self.webcam_spin.value(),
            control=self.control_kind.currentText(),
            camera_ip=self.ip_edit.text(),
            visca_port=self.port_spin.value(),
            visca_raw=self.raw_check.isChecked(),
            auto_pick=self.auto_pick.isChecked(),
            tracking=asdict(self.settings),
            stage=asdict(self.stage),
        )
        save_config(self.cfg)

    def closeEvent(self, e):
        self._save()
        self.disconnect_camera()
        self.worker.stop()
        if self.finder:
            self.finder.close()
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("PTZ Tracker")
    app.setStyle("Fusion")
    app.setPalette(dark_palette())
    app.setStyleSheet(STYLE)
    icon = resource_path("assets", "icon.png")
    if os.path.exists(icon):
        app.setWindowIcon(QIcon(icon))
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
