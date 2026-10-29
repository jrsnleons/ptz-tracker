"""PTZ Tracker for macOS — auto head tracking + manual PTZ control in one window."""
import json
import os
import sys
import threading
import time
from dataclasses import asdict, fields

import numpy as np
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QStackedWidget,
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
        self.setMinimumSize(640, 360)
        self.setStyleSheet("background:#111")
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
        p.fillRect(self.rect(), QColor("#111"))
        if self.image is None:
            p.setPen(QColor("#888"))
            p.setFont(QFont("Helvetica", 16))
            p.drawText(self.rect(), Qt.AlignCenter, "No video — pick a source and press Connect")
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
            color = QColor(255, 170, 0) if d["lost"] else QColor(0, 230, 90)
            br = box_rect(d["box"])
            p.setPen(QPen(color, 3))
            p.drawRect(br)
            p.drawLine(br.center(), QPointF(tx, ty))

        pan, tilt, zoom = d["out"]
        det_ms, loop_ms, age_ms = d.get("timing", (0, 0, 0))
        hud = f"{d['fps']:.0f} fps  det {det_ms:.0f} ms  loop {loop_ms:.0f} ms  wait {age_ms:.0f} ms   heads {len(d['dets'])} (+{d['ignored']} ignored)   pan {pan:+.2f}  tilt {tilt:+.2f}  zoom {zoom:+.2f}"
        p.setFont(QFont("Menlo", 12))
        hud_rect = p.fontMetrics().boundingRect(hud).adjusted(-8, -5, 8, 5)
        hud_rect.moveTopLeft(QPointF(r.x() + 8, r.y() + 8).toPoint())
        p.fillRect(hud_rect, QColor(0, 0, 0, 170))
        p.setPen(QColor(255, 255, 255))
        p.drawText(hud_rect, Qt.AlignCenter, hud)

        state = d.get("stage_state")
        banner = None
        if self.draw_mode:
            banner = ("Drag a box around the stage", QColor(40, 110, 200, 220))
        elif self.edit_mode:
            banner = ("Editing stage — drag an edge or corner, or the middle to move it", QColor(40, 110, 200, 220))
        elif state == "homing":
            banner = ("Going back to the stage shot…", QColor(200, 120, 0, 220))
        elif state == "waiting":
            banner = ("At stage shot — waiting for someone on stage", QColor(200, 120, 0, 220))
        if banner:
            p.setFont(QFont("Helvetica", 15, QFont.Bold))
            br = p.fontMetrics().boundingRect(banner[0]).adjusted(-14, -8, 14, 8)
            br.moveCenter(QPointF(r.center().x(), r.bottom() - 30).toPoint())
            p.fillRect(br, banner[1])
            p.setPen(QColor(255, 255, 255))
            p.drawText(br, Qt.AlignCenter, banner[0])

        if self._drag is not None:
            (x1, y1), (x2, y2) = self._drag
            p.setPen(QPen(QColor(255, 200, 0), 2))
            p.fillRect(box_rect((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))), QColor(255, 200, 0, 40))
            p.drawRect(box_rect((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))))


# --------------------------------------------------------------------------- helpers


def hold_button(text, on_press, on_release, size=46):
    b = QPushButton(text)
    b.setFixedSize(size, size)
    b.setFocusPolicy(Qt.NoFocus)
    b.pressed.connect(on_press)
    b.released.connect(on_release)
    b.setStyleSheet("font-size:18px;")
    return b


def slider(minv, maxv, value, on_change, scale=100.0):
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    s = QSlider(Qt.Horizontal)
    s.setRange(int(minv * scale), int(maxv * scale))
    s.setValue(int(value * scale))
    s.setFocusPolicy(Qt.NoFocus)
    s.setMinimumWidth(120)
    lbl = QLabel(f"{value:.2f}")
    lbl.setFixedWidth(34)

    def changed(v):
        lbl.setText(f"{v / scale:.2f}")
        on_change(v / scale)

    s.valueChanged.connect(changed)
    lay.addWidget(s)
    lay.addWidget(lbl)
    return row


# --------------------------------------------------------------------------- main window


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

        panel = QWidget()
        pl = QVBoxLayout(panel)
        pl.addWidget(self._connection_box())
        pl.addWidget(self._tracking_box())
        pl.addWidget(self._stage_box())
        pl.addWidget(self._manual_box())
        pl.addWidget(self._preset_box())
        pl.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(470)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        central = QWidget()
        lay = QHBoxLayout(central)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addWidget(self.view, 1)
        lay.addWidget(scroll)
        self.setCentralWidget(central)
        self.resize(1400, 800)

        self.status_timer = QTimer(self, interval=1000, timeout=self._refresh_status)
        self.status_timer.start()
        if self.finder:
            QTimer.singleShot(1500, self._refresh_ndi)
        self.worker.start()
        self.statusBar().showMessage(f"Detector running on {self.detector.provider}")
        QApplication.instance().installEventFilter(self)

    def _show_frame(self, data):
        self.view.show_data(data)
        self.view.repaint()
        self.worker.gui_busy = False

    # ---------------- connection
    def _connection_box(self):
        g = QGroupBox("Connection")
        f = QFormLayout(g)

        self.video_kind = QComboBox()
        self.video_kind.addItems(["NDI", "RTSP", "Webcam"])
        self.video_kind.setCurrentText(self.cfg["video"])
        f.addRow("Video", self.video_kind)

        self.video_stack = QStackedWidget()
        ndi_row = QWidget()
        nl = QHBoxLayout(ndi_row)
        nl.setContentsMargins(0, 0, 0, 0)
        self.ndi_combo = QComboBox()
        self.ndi_combo.setEditable(True)
        self.ndi_combo.setCurrentText(self.cfg["ndi_name"])
        self.ndi_combo.setMinimumWidth(180)
        refresh = QPushButton("↻")
        refresh.setFixedWidth(32)
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
        f.addRow("Source", self.video_stack)

        self.ndi_hq = QCheckBox("Full-quality NDI (more latency/CPU)")
        self.ndi_hq.setChecked(self.cfg["ndi_high_bandwidth"])
        f.addRow("", self.ndi_hq)

        self.control_kind = QComboBox()
        self.control_kind.addItems(["VISCA over IP", "NDI PTZ", "None"])
        self.control_kind.setCurrentText(self.cfg["control"])
        f.addRow("Control", self.control_kind)
        self.ip_edit = QLineEdit(self.cfg["camera_ip"])
        f.addRow("Camera IP", self.ip_edit)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(self.cfg["visca_port"])
        f.addRow("VISCA port", self.port_spin)
        self.raw_check = QCheckBox("Raw VISCA (no IP header)")
        self.raw_check.setChecked(self.cfg["visca_raw"])
        f.addRow("", self.raw_check)

        btns = QHBoxLayout()
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self.connect_camera)
        disc = QPushButton("Disconnect")
        disc.clicked.connect(self.disconnect_camera)
        btns.addWidget(self.connect_btn)
        btns.addWidget(disc)
        f.addRow(btns)
        self.conn_status = QLabel("Not connected")
        self.conn_status.setWordWrap(True)
        self.conn_status.setStyleSheet("color:#888")
        f.addRow(self.conn_status)
        return g

    def _refresh_ndi(self):
        if not self.finder:
            self.statusBar().showMessage("NDI not available (cyndilib missing)")
            return
        current = self.ndi_combo.currentText()
        names = self.finder.names()
        self.ndi_combo.clear()
        self.ndi_combo.addItems(names)
        if current:
            self.ndi_combo.setCurrentText(current)
        self.statusBar().showMessage(f"Found {len(names)} NDI source(s)")

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
            QMessageBox.warning(self, "Connect failed", str(e))
            return
        ip = self.ip_edit.text().strip()
        # position readback for the stage boundary (Canon HTTP API, read-only)
        self.poller = CanonPositionPoller(ip) if ip else None
        self.worker.set_io(self.source, self.cam, self.poller)
        self._save()
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

    def _refresh_status(self):
        video = f"Video: {self.source.name}" if self.source else "Video: none"
        self.conn_status.setText(f"{video}\n{self.cam.status()}")
        pos = self.poller.status() if self.poller else "position: not connected"
        b = self.stage.get_bounds()
        if b is None:
            stage = "No stage drawn yet"
        else:
            stage = f"Stage: pan {b.pan_min:+.1f}° … {b.pan_max:+.1f}°, tilt {b.tilt_min:+.1f}° … {b.tilt_max:+.1f}°"
        self.stage_status.setText(f"{pos}\n{stage}")

    # ---------------- stage boundary
    def _stage_box(self):
        g = QGroupBox("Stage boundary")
        v = QVBoxLayout(g)
        self.stage_check = QCheckBox("Return to stage shot when the speaker leaves the stage")
        self.stage_check.setChecked(self.stage.enabled)
        self.stage_check.toggled.connect(lambda on: setattr(self.stage, "enabled", on))
        v.addWidget(self.stage_check)

        row = QHBoxLayout()
        draw = QPushButton("Draw stage…")
        draw.setToolTip("Zoom out to the wide stage shot, then drag a box around the stage")
        draw.setFocusPolicy(Qt.NoFocus)
        draw.clicked.connect(self.start_stage_draw)
        home = QPushButton("Go to stage shot")
        home.setFocusPolicy(Qt.NoFocus)
        home.clicked.connect(self.go_stage_home)
        self.edit_btn = QPushButton("Edit stage")
        self.edit_btn.setCheckable(True)
        self.edit_btn.setToolTip("Drag the stage edges or corners (or the middle to move the whole box) instead of redrawing it")
        self.edit_btn.setFocusPolicy(Qt.NoFocus)
        self.edit_btn.toggled.connect(self.set_edit_stage)
        row.addWidget(draw)
        row.addWidget(self.edit_btn)
        row.addWidget(home)
        v.addLayout(row)

        self.resume_check = QCheckBox("Resume tracking when someone is back on stage")
        self.resume_check.setChecked(self.stage.auto_resume)
        self.resume_check.toggled.connect(lambda on: setattr(self.stage, "auto_resume", on))
        v.addWidget(self.resume_check)

        self.resume_move_check = QCheckBox("When they return, ease to a set zoom while tracking")
        self.resume_move_check.setChecked(self.stage.resume_move)
        self.resume_move_check.toggled.connect(self._resume_move_toggled)
        v.addWidget(self.resume_move_check)
        row2 = QHBoxLayout()
        set_resume = QPushButton("Set resume zoom from current view")
        set_resume.setToolTip("Zoom to the level you want when someone comes back, then press this. Only the zoom is used; the position follows the person")
        set_resume.setFocusPolicy(Qt.NoFocus)
        set_resume.clicked.connect(self.set_resume_shot)
        row2.addWidget(set_resume)
        v.addLayout(row2)

        f = QFormLayout()
        f.addRow("Return to stage speed", slider(0.1, 1.0, self.stage.home_speed, lambda x: setattr(self.stage, "home_speed", x)))
        f.addRow("Resume zoom speed", slider(0.1, 1.0, self.stage.resume_speed, lambda x: setattr(self.stage, "resume_speed", x)))
        spin = QSpinBox()
        spin.setRange(1, 99)
        spin.setValue(self.stage.home_preset)
        spin.valueChanged.connect(lambda n: setattr(self.stage, "home_preset", n))
        f.addRow("Stage shot preset", spin)
        f.addRow("Left stage for (s)", slider(0.5, 10, self.stage.leave_seconds, lambda x: setattr(self.stage, "leave_seconds", x)))
        f.addRow("Lost for (s)", slider(0.5, 10, self.stage.lost_seconds, lambda x: setattr(self.stage, "lost_seconds", x)))
        v.addLayout(f)

        self.stage_status = QLabel()
        self.stage_status.setWordWrap(True)
        self.stage_status.setStyleSheet("color:#888")
        v.addWidget(self.stage_status)
        return g

    def start_stage_draw(self):
        pos = self.poller.latest() if self.poller else None
        if pos is None or self.view.image is None:
            QMessageBox.information(
                self, "Draw stage", "Connect to the camera first. The app needs video and the camera's position (Camera IP)."
            )
            return
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

    # ---------------- tracking
    def _tracking_box(self):
        g = QGroupBox("Auto tracking")
        v = QVBoxLayout(g)
        self.track_btn = QPushButton("Start tracking  (Space)")
        self.track_btn.setCheckable(True)
        self.track_btn.setMinimumHeight(44)
        self.track_btn.setFocusPolicy(Qt.NoFocus)
        self.track_btn.toggled.connect(self.set_tracking)
        v.addWidget(self.track_btn)

        row = QHBoxLayout()
        self.auto_pick = QCheckBox("Auto-pick biggest head")
        self.auto_pick.setChecked(self.cfg["auto_pick"])
        self.auto_pick.toggled.connect(lambda on: setattr(self.worker, "auto_pick", on))
        rel = QPushButton("Release target (Esc)")
        rel.setFocusPolicy(Qt.NoFocus)
        rel.clicked.connect(self.worker.release_target)
        row.addWidget(self.auto_pick)
        row.addWidget(rel)
        v.addLayout(row)
        hint = QLabel("Click a head in the video to follow that person. Heads in the shaded bottom zone (audience) are ignored.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#888")
        v.addWidget(hint)

        locks = QHBoxLayout()
        s = self.settings
        for label, attr in (("Lock pan", "lock_horizontal"), ("Lock tilt", "lock_vertical"), ("Lock zoom", "lock_zoom")):
            c = QCheckBox(label)
            c.setChecked(getattr(s, attr))
            c.toggled.connect(lambda on, a=attr: setattr(self.settings, a, on))
            locks.addWidget(c)
        v.addLayout(locks)

        f = QFormLayout()
        for label, attr, lo, hi in (
            ("Speed", "speed", 0.1, 3.0),
            ("Max speed", "max_speed", 0.05, 1.0),
            ("Deadzone ↔", "deadzone_x", 0.0, 0.3),
            ("Deadzone ↕", "deadzone_y", 0.0, 0.3),
            ("Curve", "exponent", 1.0, 3.0),
            ("Smoothing", "smoothing", 0.05, 1.0),
            ("Frame X", "target_x", 0.1, 0.9),
            ("Frame Y", "target_y", 0.1, 0.9),
            ("Head size", "target_height", 0.03, 0.5),
            ("Ignore below", "ignore_below", 0.3, 1.0),
        ):
            f.addRow(label, slider(lo, hi, getattr(s, attr), lambda val, a=attr: setattr(self.settings, a, val)))
        v.addLayout(f)
        return g

    def set_tracking(self, on):
        if self.track_btn.isChecked() != on:
            self.track_btn.setChecked(on)  # re-enters via toggled
            return
        self.worker.tracking_enabled = on
        self.track_btn.setText("Stop tracking  (Space)" if on else "Start tracking  (Space)")
        self.track_btn.setStyleSheet("background:#1f8f4e;color:white;font-weight:bold" if on else "")

    # ---------------- manual control
    def _manual_box(self):
        g = QGroupBox("Manual control")
        v = QVBoxLayout(g)
        top = QHBoxLayout()

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
        home = QPushButton("⌂")
        home.setFixedSize(46, 46)
        home.setToolTip("Home (H)")
        home.setFocusPolicy(Qt.NoFocus)
        home.clicked.connect(lambda: self.cam.home())
        pad.addWidget(home, 1, 1)
        top.addLayout(pad)

        side = QGridLayout()
        side.addWidget(QLabel("Zoom"), 0, 0, 1, 2, Qt.AlignCenter)
        side.addWidget(hold_button("−", lambda: self.zoom_press(-1), self.zoom_release), 1, 0)
        side.addWidget(hold_button("+", lambda: self.zoom_press(1), self.zoom_release), 1, 1)
        side.addWidget(QLabel("Focus"), 2, 0, 1, 2, Qt.AlignCenter)
        side.addWidget(hold_button("N", lambda: self.cam.focus(-0.5), lambda: self.cam.focus(0)), 3, 0)
        side.addWidget(hold_button("F", lambda: self.cam.focus(0.5), lambda: self.cam.focus(0)), 3, 1)
        top.addLayout(side)
        v.addLayout(top)

        af = QHBoxLayout()
        b = QPushButton("Auto focus")
        b.clicked.connect(lambda: self.cam.autofocus(True))
        af.addWidget(b)
        b = QPushButton("One-push AF")
        b.clicked.connect(lambda: self.cam.one_push_af())
        af.addWidget(b)
        b = QPushButton("STOP")
        b.setStyleSheet("background:#b3261e;color:white;font-weight:bold")
        b.clicked.connect(self.emergency_stop)
        af.addWidget(b)
        v.addLayout(af)

        f = QFormLayout()
        f.addRow("Manual speed", slider(0.05, 1.0, self.cfg["manual_speed"], lambda val: self.cfg.__setitem__("manual_speed", val)))
        v.addLayout(f)
        keys = QLabel("Keys: arrows / WASD pan-tilt · Q/E zoom · H home · Space track · 1-9 recall · ⇧1-9 store")
        keys.setWordWrap(True)
        keys.setStyleSheet("color:#888")
        v.addWidget(keys)
        return g

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

    # ---------------- presets
    def _preset_box(self):
        g = QGroupBox("Presets")
        grid = QGridLayout(g)
        for i in range(9):
            b = QPushButton(str(i + 1))
            b.setMinimumHeight(36)
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(lambda _=False, n=i + 1: self.preset(n))
            grid.addWidget(b, i // 5, i % 5)
        self.store_btn = QPushButton("Store…")
        self.store_btn.setCheckable(True)
        self.store_btn.setMinimumHeight(36)
        self.store_btn.setFocusPolicy(Qt.NoFocus)
        self.store_btn.toggled.connect(self._store_toggled)
        grid.addWidget(self.store_btn, 1, 4)
        return g

    def _store_toggled(self, on):
        self.store_mode = on
        self.store_btn.setStyleSheet("background:#c77700;color:white" if on else "")
        if on:
            self.statusBar().showMessage("Store mode: press a preset number to save the current position")

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
        if not press:
            return False
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
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
