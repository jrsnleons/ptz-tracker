"""PTZ Tracker — auto head tracking + manual PTZ control in one window (macOS and Windows)."""
import faulthandler
import json
import logging
import os
import sys
import threading
import time
from dataclasses import asdict, fields

import numpy as np
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QCursor, QDesktopServices, QFont, QIcon, QImage, QPainter, QPalette, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import keys, motion, picture, platform_paths, updater
from . import ptz as ptzmod
from .detector import HeadDetector, default_model_path
from .sources import HAVE_NDI, CVSource, NDIFinder, NDISource
from .stage import CanonPositionPoller, Homing, StageBounds, StageSettings, angle_to_pixel, pixel_to_angle
from .tracking import CamController, TargetTracker, TrackSettings
from .version import __version__

CONFIG_DIR = platform_paths.config_dir()
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
PERF_LOG = os.path.join(platform_paths.log_dir(), "perf.log")
APP_LOG = os.path.join(platform_paths.log_dir(), "app.log")
NO_VIDEO_HINT = 6.0  # connected this long with no picture -> say what to check
log = logging.getLogger("ptztracker")
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
    "zoom_scaled_speed": True,  # slow pan/tilt down when zoomed in
    "keys": {},  # action -> [keys]; missing actions use keys.DEFAULTS
    "preset_names": {},  # "3": "Pulpit"
    "picture": {},  # camera -> last picture values sent
    "looks": {},  # name -> picture values
    "update_auto": True,
    "update_skip": "",
    "auto_pick": True,
    "tracking": asdict(TrackSettings()),
    "stage": asdict(StageSettings()),
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH, encoding="utf-8-sig") as f:  # -sig: Notepad/PowerShell add a BOM
            saved = json.load(f)
        tracking = {**cfg["tracking"], **saved.pop("tracking", {})}
        stage = {**cfg["stage"], **saved.pop("stage", {})}
        cfg.update(saved)
        cfg["tracking"] = tracking
        cfg["stage"] = stage
    except (OSError, ValueError):
        pass
    return motion.migrate_speed(cfg)


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
        self._err_at = 0.0
        self._perf = None
        try:
            os.makedirs(os.path.dirname(PERF_LOG), exist_ok=True)
            self._perf = open(PERF_LOG, "a", buffering=1)
        except OSError:
            pass

    def _report(self, msg):
        """Logs the current exception and shows it in the status bar, at most every few seconds so a repeating
        one doesn't flood either."""
        now = time.monotonic()
        if now - self._err_at > 3:
            self._err_at = now
            log.error(msg, exc_info=True)
            self.error.emit(msg)

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
                self._report(f"Video error: {e}")
                time.sleep(0.5)
                continue
            if frame is None:
                self._idle(cam, src)
                continue
            try:
                self._stalled = False
                h, w = frame.shape[:2]
                now = time.monotonic()
                t0 = time.perf_counter()
                try:
                    all_dets = self.detector.detect(frame)
                except Exception as e:
                    was = self.detector.provider
                    if self.detector.use_cpu():
                        self._report(f"Detection failed on {was}, switched to CPU")
                    else:
                        self._report(f"Detection error: {e}")
                    all_dets = np.zeros((0, 5), np.float32)
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
            except Exception as e:  # never let one bad frame kill the video thread
                self._report(f"Tracking error: {e}")
                if not self.gui_busy:  # still show the picture
                    self.gui_busy = True
                    self.frame_ready.emit(
                        {"frame": frame, "dets": np.zeros((0, 5), np.float32), "ignored": 0, "box": None, "lost": False,
                         "out": (0.0, 0.0, 0.0), "fps": fps, "timing": timing, "pos": None, "bounds": None,
                         "off_stage": 0, "stage_state": None}
                    )  # fmt: skip
                time.sleep(0.05)


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


# --------------------------------------------------------------------------- look

INK, INK2, INK3 = "#EDEEF0", "#A3A7AD", "#8A8F96"
GROUND, PANEL, RAISED, LINE = "#121314", "#1B1C1E", "#242629", "#303236"
GREEN, RED, AMBER = "#3DD68C", "#F2555A", "#F5A524"
if platform_paths.IS_WIN:
    UI_FONT, MONO_FONT, MOD = "Segoe UI Variable Text", "Cascadia Mono", "Ctrl"
else:
    UI_FONT, MONO_FONT, MOD = ".AppleSystemUIFont", "Menlo", "⌘"

STYLE = f"""
QWidget {{ color: {INK}; font-size: 13px; font-family: "{UI_FONT}"; }}
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
QPushButton#preset {{ font-size: 14px; font-weight: 600; padding: 4px 2px; }}
QPushButton#seg {{ padding: 4px 0; border-radius: 6px; font-weight: 600; }}
QPushButton#seg:checked {{ background: {INK}; color: {GROUND}; border: 0; }}
QPushButton#keybtn {{ min-width: 84px; padding: 4px 8px; }}
QPushButton#keybtn:checked {{ border: 1px solid {AMBER}; color: {AMBER}; }}
QFrame#updatebar {{ background: #1F2A3D; border-bottom: 1px solid {LINE}; }}
QProgressBar {{ background: {RAISED}; border: 0; border-radius: 4px; max-height: 8px; }}
QProgressBar::chunk {{ background: {GREEN}; border-radius: 4px; }}
QPushButton#link {{ background: transparent; border: 0; color: {INK2}; padding: 2px 4px; font-size: 12px; }}
QPushButton#link:hover {{ color: {INK}; }}
QPushButton#link:checked {{ color: {AMBER}; font-weight: 600; }}
QPushButton#link:disabled, QPushButton#store:disabled, QLabel#muted:disabled {{ color: #5A5D63; }}
QPushButton#chip {{ font-size: 12px; padding: 4px 12px; }}
QLabel#section {{ color: {INK2}; font-size: 12px; font-weight: 600; }}
QLabel#hint {{ color: {INK3}; font-size: 11px; }}
QLabel#muted {{ color: {INK2}; }}
QLabel#perf {{ color: {INK2}; font-family: "{MONO_FONT}", Consolas, monospace; font-size: 12px; }}
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
    """Files shipped with the app: repo root from source, Contents/Resources in the .app, _internal on Windows."""
    return os.path.join(platform_paths.resource_dir(), *parts)


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


def slider(minv, maxv, value, on_change, scale=100.0, fmt=None):
    """Slider with its value at the right; the row exposes .slider for resets and .set(value) to move it silently.
    fmt turns the value into the text shown at the right."""
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    s = QSlider(Qt.Horizontal)
    s.setRange(round(minv * scale), round(maxv * scale))
    s.setValue(round(value * scale))
    s.setFocusPolicy(Qt.NoFocus)
    s.setMinimumWidth(120)
    text = fmt or (lambda x: f"{x:.2f}")
    lbl = label(text(value), "muted")
    lbl.setFixedWidth(36 if fmt is None else 54)
    lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def changed(v):
        lbl.setText(text(v / scale))
        on_change(v / scale)

    def set_value(x):
        s.blockSignals(True)
        s.setValue(round(x * scale))
        s.blockSignals(False)
        lbl.setText(text(x))

    s.valueChanged.connect(changed)
    lay.addWidget(s)
    lay.addWidget(lbl)
    row.slider, row.scale, row.set = s, scale, set_value
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


class KeyButton(QPushButton):
    """Shows a key; click it, then press the new key. Esc cancels, Backspace/Delete clears."""

    captured = Signal(int)
    cleared = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("keybtn")
        self.setCheckable(True)
        self.shown = ""
        self.toggled.connect(self._toggled)

    def set_key(self, text):
        self.shown = text
        if not self.isChecked():
            self.setText(text or "—")

    def _toggled(self, on):
        if on:
            self.setText("Press a key…")
            self.setFocus()
        else:
            self.setText(self.shown or "—")

    def event(self, e):
        if self.isChecked() and e.type() == QEvent.KeyPress:  # also takes Tab and Space while listening
            k = int(e.key())
            if k in keys.MODIFIER_KEYS:
                return True
            self.setChecked(False)
            if k in (int(Qt.Key_Backspace), int(Qt.Key_Delete)):
                self.cleared.emit()
            elif k != int(Qt.Key_Escape):
                self.captured.emit(k)
            return True
        return super().event(e)

    def focusOutEvent(self, e):
        self.setChecked(False)
        super().focusOutEvent(e)


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
    canon_probed = Signal(dict)  # picture items found on the camera (from a background thread)
    update_checked = Signal(object, str, bool)  # release or None, error, was it a manual check
    update_progress = Signal(int)
    update_ready = Signal(str, str)  # downloaded file, error

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
        self.binds = keys.Binds(self.cfg.get("keys"))
        self._relabels = []  # callables that rewrite key hints on buttons after a bind change
        self.speed_level = self.cfg["speed_level"]
        self.levels = self.cfg["speed_levels"]
        self.ramp = motion.Ramp()
        self._ramp_t = 0.0
        self._sent_pt = (0.0, 0.0)
        self._widest_hfov = 0.0
        self.ramp_timer = QTimer(self, interval=33, timeout=self._manual_tick)  # ~30 Hz, only runs while moving
        self.bank = 0  # preset bank the number keys address: 0 = slots 1-10, 1 = 11-20
        self.pic = picture.clean(None)
        self.pic_key = ""
        self.pic_dirty = set()
        self.pic_timer = QTimer(self, singleShot=True, interval=100, timeout=self._pic_flush)
        self.canon = None
        self.release = None  # newer Release found by the update check
        self._checking = False
        self._updating = False
        self._perf_at = 0.0
        self._connected_at = 0.0

        self.detector = HeadDetector(default_model_path())
        self.worker = TrackerWorker(self.detector, self.settings, self.stage)
        self.worker.auto_pick = self.cfg["auto_pick"]

        self.view = VideoView(self.settings)
        self.view.clicked.connect(self.worker.click)
        self.view.drag_move.connect(lambda pan, tilt: self.manual_press("mouse", pan, tilt))
        self.view.drag_end.connect(lambda: self.manual_release("mouse"))
        self.view.wheel_zoom.connect(self._wheel_zoom)
        self.worker.frame_ready.connect(self._show_frame)
        self.worker.error.connect(self._status)
        self.worker.stage_event.connect(self._status)
        self.worker.tracking_stopped.connect(lambda: self.set_tracking(False))
        self.view.rect_drawn.connect(self.stage_drawn)
        self.view.bounds_edited.connect(self._bounds_edited)
        self.view.bounds_edit_done.connect(self._bounds_edit_done)

        self.canon_probed.connect(self._canon_ready)
        self.update_checked.connect(self._update_checked)
        self.update_progress.connect(self._update_progress)
        self.update_ready.connect(self._update_ready)
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
        outer.addWidget(self._update_bar())
        outer.addLayout(body, 1)
        self.setCentralWidget(central)

        self.stage_status = label("", "muted")
        self.statusBar().addPermanentWidget(self.stage_status)
        self.statusBar().setSizeGripEnabled(False)
        self.resize(1440, 900)

        self._set_connected(False)
        self.set_tracking(False)
        self.set_speed_level(self.speed_level, toast=False)
        self._relabel_all()
        self.status_timer = QTimer(self, interval=1000, timeout=self._refresh_status)
        self.status_timer.start()
        if self.finder:
            QTimer.singleShot(1500, self._refresh_ndi)
        if not os.environ.get("PTZ_NO_UPDATE_CHECK"):
            QTimer.singleShot(10_000, self._auto_check)  # not at launch, so it never slows startup
            QTimer(self, interval=6 * 3600 * 1000, timeout=self._auto_check).start()
        self.worker.start()
        self.statusBar().showMessage(f"Detector ready on {self.detector.provider.replace('ExecutionProvider', '')}")
        QApplication.instance().installEventFilter(self)

    def _status(self, msg):
        self.statusBar().showMessage(msg)

    def _show_frame(self, data):
        try:
            self.view.show_data(data)
            self.view.repaint()
        except Exception:
            log.exception("could not draw a frame")
        finally:
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
        gear.setToolTip(f"Settings ({MOD}+,)" if MOD == "Ctrl" else "Settings (⌘,)")
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
        self.card_status = label("", "muted", wrap=True)
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
        msg = f"Found {len(names)} NDI source{'s' if len(names) != 1 else ''} on the network"
        if not names and platform_paths.IS_WIN:
            msg += ". If the camera is on, allow PTZ Tracker through Windows Firewall, or type its name above."
        self.card_status.setText(msg)

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
                self.cam = ptzmod.Guarded(ptzmod.VISCAoverIP(self.ip_edit.text().strip(), self.port_spin.value(), self.raw_check.isChecked()))
            elif ctrl == "NDI PTZ":
                if not isinstance(self.source, NDISource):
                    raise RuntimeError("NDI PTZ needs the video source to be NDI")
                self.cam = ptzmod.Guarded(ptzmod.NDIPTZ(self.source))
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
        self._widest_hfov = 0.0
        self._picture_connected(ip)
        self._save()
        self._set_connected(True)
        self._connected_at = time.monotonic()
        self.statusBar().showMessage(f"Connected to {self.source.name}")

    def disconnect_camera(self):
        self.set_tracking(False)
        self._manual_reset()
        self.held.clear()
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
        self.canon = None
        self.view.image = None
        self.perf.setText("")
        self._picture_refresh()
        self._set_connected(False)

    def _no_video_hint(self):
        if self.source is None or self.view.image is not None:
            return
        if time.monotonic() - self._connected_at < NO_VIDEO_HINT:
            return
        if isinstance(self.source, NDISource):
            fw = " Allow PTZ Tracker through Windows Firewall (Private networks)." if platform_paths.IS_WIN else ""
            hint = f"No video from {self.source.name} yet. Check the camera is on and on this network.{fw}"
        else:
            hint = f"No video from {self.source.name} yet. Close other apps using it, or try another source."
        hint += f"\n\nDetails are logged in {APP_LOG}"
        if self.view.placeholder != hint:
            self.view.placeholder = hint
            self.view.update()
            log.warning("connected to %s but no frames after %.0f s", self.source.name, NO_VIDEO_HINT)

    def _refresh_status(self):
        self._no_video_hint()
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
        stop = nofocus(QPushButton())
        stop.setObjectName("stop")
        stop.setMinimumHeight(48)
        stop.clicked.connect(self.emergency_stop)
        v.addWidget(stop)

        def relabel():
            k = self.binds.short("stop")
            stop.setText(f"Stop camera    {k}".rstrip())
            stop.setToolTip(f"Stops tracking and every camera move ({k})")

        self._on_binds(relabel)
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
        rel = nofocus(QPushButton())
        rel.clicked.connect(self.worker.release_target)

        def relabel():
            k = self.binds.short("release")
            rel.setText(f"Release  {k}".rstrip())
            rel.setToolTip(f"Stop following this person so another can be picked ({k})")

        self._on_binds(relabel)
        self._on_binds(lambda: self.track_btn.key.setText(self.binds.short("track")))
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
        home = nofocus(QPushButton())
        home.setMinimumHeight(40)
        home.setStyleSheet("font-weight: 600;")
        home.clicked.connect(self.go_stage_home)
        v.addWidget(home)

        def relabel():
            k = self.binds.short("stage_shot")
            home.setText(f"Go to stage shot    {k}".rstrip())
            home.setToolTip(f"Stop tracking and go back to the wide stage shot ({k})")

        self._on_binds(relabel)
        self.stage_check = QCheckBox("Return to the stage shot when the speaker leaves")
        self.stage_check.setChecked(self.stage.enabled)
        self.stage_check.toggled.connect(lambda on: setattr(self.stage, "enabled", on))
        v.addWidget(self.stage_check)
        self.resume_check = QCheckBox("Resume tracking when someone is back on stage")
        self.resume_check.setChecked(self.stage.auto_resume)
        self.resume_check.toggled.connect(lambda on: setattr(self.stage, "auto_resume", on))
        v.addWidget(self.resume_check)
        return v

    def _stage_preset_changed(self, n):
        self.stage.home_preset = n
        self._refresh_presets()

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

    # ---------------- key hints
    def _on_binds(self, fn):
        """Registers a function that writes key hints into widgets; it reruns whenever the binds change."""
        self._relabels.append(fn)
        fn()

    def _relabel_all(self):
        for fn in self._relabels:
            fn()

    def _binds_changed(self, notes=None):
        self._relabel_all()
        self._refresh_key_rows(notes)
        self._refresh_presets()
        self._save()

    # ---------------- presets
    def _preset_box(self):
        v = QVBoxLayout()
        v.setSpacing(8)
        self.bank_hint = label("", "hint")
        v.addLayout(section("Presets", self.bank_hint))
        grid = QGridLayout()
        grid.setSpacing(6)
        self.preset_btns = []
        for i in range(20):
            b = nofocus(QPushButton(str(i + 1)))
            b.setObjectName("preset")
            b.setFixedHeight(46)
            b.clicked.connect(lambda _=False, n=i + 1: self.preset(n))
            b.setContextMenuPolicy(Qt.CustomContextMenu)
            b.customContextMenuRequested.connect(lambda _pos, n=i + 1: self._preset_menu(n))
            grid.addWidget(b, i // 5, i % 5)
            self.preset_btns.append(b)
        v.addLayout(grid)
        row = QHBoxLayout()
        self.store_btn = nofocus(QPushButton("Store"))
        self.store_btn.setObjectName("store")
        self.store_btn.setCheckable(True)
        self.store_btn.setMinimumHeight(40)
        self.store_btn.setToolTip("Then press a preset to save the current shot there (or hold Shift and press its number)")
        self.store_btn.toggled.connect(self._store_toggled)
        self.bank_btn = nofocus(QPushButton())
        self.bank_btn.setMinimumHeight(40)
        self.bank_btn.clicked.connect(self.toggle_bank)
        row.addWidget(self.store_btn, 1)
        row.addWidget(self.bank_btn, 1)
        v.addLayout(row)
        self._on_binds(self._refresh_presets)
        return v

    def _refresh_presets(self):
        names, stage_n = self.cfg["preset_names"], self.stage.home_preset
        for i, b in enumerate(self.preset_btns):
            n = i + 1
            name = "Stage" if n == stage_n else names.get(str(n), "")
            short = name if len(name) <= 8 else name[:7] + "…"
            b.setText(f"{n}\n{short}" if name else str(n))
            active = i // 10 == self.bank
            css = []
            if n == stage_n:
                css.append(f"border: 1px solid {AMBER}; color: {AMBER};")
            if not active:
                css.append(f"background: {PANEL}; " + ("" if n == stage_n else f"color: {INK3};"))
            b.setStyleSheet("QPushButton { %s }" % " ".join(css) if css else "")
            tip = f"Preset {n}: click to recall, right-click to rename"
            b.setToolTip(tip + ("\nThis is the stage shot" if n == stage_n else "") + (f"\n{names[str(n)]}" if str(n) in names else ""))
        lo = self.bank * 10 + 1
        self.bank_hint.setText(f"Keys → {lo}–{lo + 9}   ⇧ stores")
        self.bank_btn.setText(f"Bank {self.bank + 1}   {self.binds.short('bank')}".rstrip())
        self.bank_btn.setToolTip("Switch which presets the number keys use (1–10 or 11–20)")

    def toggle_bank(self):
        self.bank = 1 - self.bank
        self._refresh_presets()
        lo = self.bank * 10 + 1
        self.view.toast(f"Preset keys {lo}–{lo + 9}")

    def _preset_menu(self, n):
        menu = QMenu(self)
        menu.addAction("Rename…", lambda: self._rename_preset(n))
        if str(n) in self.cfg["preset_names"]:
            menu.addAction("Clear name", lambda: self._set_preset_name(n, ""))
        menu.exec(QCursor.pos())

    def _rename_preset(self, n):
        text, ok = QInputDialog.getText(self, "Rename preset", f"Name for preset {n}", text=self.cfg["preset_names"].get(str(n), ""))
        if ok:
            self._set_preset_name(n, text)

    def _set_preset_name(self, n, text):
        text = text.strip()[:24]
        if text:
            self.cfg["preset_names"][str(n)] = text
        else:
            self.cfg["preset_names"].pop(str(n), None)
        self._refresh_presets()
        self._save()

    def _store_toggled(self, on):
        self.store_mode = on
        if on:
            self.statusBar().showMessage("Store mode: press a preset to save the current shot")

    def preset(self, n, store=None):
        store = self.store_mode if store is None else store
        if store:
            if n == self.stage.home_preset and QMessageBox.question(
                self, "Replace the stage shot?", f"Preset {n} is the stage shot. Storing here replaces it. Continue?"
            ) != QMessageBox.Yes:
                self.store_btn.setChecked(False)
                return
            self.cam.store_preset(n)
            self.statusBar().showMessage(f"Stored preset {n}")
            self.store_btn.setChecked(False)
        else:
            self.set_tracking(False)
            self._manual_reset()
            self.cam.recall_preset(n)
            name = self.cfg["preset_names"].get(str(n))
            self.statusBar().showMessage(f"Recalled preset {n}" + (f" ({name})" if name else ""))

    # ---------------- manual control
    def _manual_box(self):
        v = QVBoxLayout()
        v.setSpacing(8)
        self.manual_hint = label("", "hint")
        v.addLayout(section("Manual", self.manual_hint))
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
        home.setStyleSheet(f"QPushButton {{ background: {PANEL}; border-radius: 20px; color: {INK2}; }} QPushButton:disabled {{ color: #5A5D63; }}")
        home.clicked.connect(self.camera_home)
        pad.addWidget(home, 1, 1)
        top.addLayout(pad)

        side = QGridLayout()
        side.setSpacing(6)
        side.addWidget(label("Zoom", "muted"), 0, 0)
        zout = hold_button("", lambda: self.zoom_press(-1), self.zoom_release, 64, 34)
        zin = hold_button("", lambda: self.zoom_press(1), self.zoom_release, 64, 34)
        side.addWidget(zout, 0, 1)
        side.addWidget(zin, 0, 2)
        side.addWidget(label("Focus", "muted"), 1, 0)
        near = hold_button("", lambda: self.cam.focus(-0.5), lambda: self.cam.focus(0), 64, 34)
        far = hold_button("", lambda: self.cam.focus(0.5), lambda: self.cam.focus(0), 64, 34)
        side.addWidget(near, 1, 1)
        side.addWidget(far, 1, 2)
        af = nofocus(QPushButton("Auto"))
        af.setToolTip("Turn on continuous auto focus")
        af.clicked.connect(lambda: self.cam.autofocus(True))
        once = nofocus(QPushButton())
        once.clicked.connect(lambda: self.cam.one_push_af())
        side.addWidget(af, 2, 1)
        side.addWidget(once, 2, 2)
        top.addLayout(side, 1)
        v.addLayout(top)

        row = QHBoxLayout()
        row.setSpacing(4)
        row.addWidget(label("Speed", "muted"))
        row.addSpacing(6)
        self.seg = []
        for i, name in enumerate(motion.LEVEL_NAMES):
            b = nofocus(QPushButton(str(i + 1)))
            b.setObjectName("seg")
            b.setCheckable(True)
            b.setFixedHeight(28)
            b.setToolTip(f"{i + 1} · {name}")
            b.clicked.connect(lambda _=False, n=i + 1: self.set_speed_level(n, toast=False))
            row.addWidget(b, 1)
            self.seg.append(b)
        v.addLayout(row)
        self.speed_hint = label("", "hint")
        v.addWidget(self.speed_hint)

        k = self.binds.short
        al = self.binds.all_text

        def relabel():
            self.manual_hint.setText("Keys " + " ".join(k(a) for a in ("tilt_up", "pan_left", "tilt_down", "pan_right")))
            home.setToolTip(f"Camera home position ({k('home')})")
            zout.setText(f"−   {k('zoom_out')}")
            zout.setToolTip(f"Zoom out ({al('zoom_out')})")
            zin.setText(f"+   {k('zoom_in')}")
            zin.setToolTip(f"Zoom in ({al('zoom_in')})")
            near.setText(f"Near  {k('focus_near')}")
            near.setToolTip(f"Focus nearer ({al('focus_near')})")
            far.setText(f"Far  {k('focus_far')}")
            far.setToolTip(f"Focus farther ({al('focus_far')})")
            once.setText(f"Once  {k('focus_once')}".rstrip())
            once.setToolTip(f"Focus once on the current shot, then hold it ({al('focus_once')})")
            self.speed_hint.setText(f"{k('speed_down')} slower · {k('speed_up')} faster")

        self._on_binds(relabel)
        return v

    def camera_home(self):
        self._manual_reset()
        self.cam.home()

    def set_speed_level(self, n, toast=True):
        n = min(max(int(n), 1), len(motion.LEVEL_NAMES))
        self.speed_level = self.cfg["speed_level"] = n
        for i, b in enumerate(self.seg):
            b.setChecked(i + 1 == n)
        if toast:
            self.view.toast(f"Speed {n} · {motion.LEVEL_NAMES[n - 1]}")
        if self.zoom_held:
            self.cam.zoom(self.zoom_held * self._level()["zoom"])
        if self.held or self.ramp.active:
            self._start_manual()

    def _level(self):
        return self.levels[self.speed_level - 1]

    def _zoom_scale(self):
        pos = self.poller.latest() if self.poller else None
        if pos is None or not self.cfg["zoom_scaled_speed"]:
            return 1.0
        self._widest_hfov = max(self._widest_hfov, pos.hfov, (self.stage.home or {}).get("hfov", 0.0))
        return motion.zoom_scale(pos.hfov, self._widest_hfov)

    def _manual_target(self):
        """Pan/tilt the held keys ask for right now: direction, speed level, diagonal and zoom scaling."""
        x, y = motion.normalise(sum(p for p, _ in self.held.values()), sum(t for _, t in self.held.values()))
        speed = self._level()["pan"] * self._zoom_scale()
        return x * speed, y * speed

    def _start_manual(self):
        self.worker.manual_active = True
        if not self.ramp_timer.isActive():
            self._ramp_t = time.monotonic() - 1 / 30
            self.ramp_timer.start()
        self._manual_tick()

    def _manual_tick(self):
        now = time.monotonic()
        dt, self._ramp_t = min(now - self._ramp_t, 0.1), now
        pan, tilt = self.ramp.step(dt, *self._manual_target())
        pt = (motion.floor_speed(pan), motion.floor_speed(tilt))
        if pt != self._sent_pt:
            self._sent_pt = pt
            self.cam.pan_tilt(*pt)
        moving = bool(self.held) or self.ramp.active
        self.worker.manual_active = moving or self.zoom_held != 0
        if not moving:
            self.ramp_timer.stop()

    def _manual_reset(self):
        """Forget any manual move in progress (a preset or home is about to take over), without sending a stop."""
        self.ramp.reset()
        self.ramp_timer.stop()
        self._sent_pt = (0.0, 0.0)
        self.worker.manual_active = bool(self.held) or self.zoom_held != 0

    def _wheel_zoom(self, direction):
        if direction:
            self.zoom_press(direction)
        else:
            self.zoom_release()

    def manual_press(self, key, pan, tilt):
        self.held[key] = (pan, tilt)
        self._start_manual()

    def manual_release(self, key):
        self.held.pop(key, None)
        self._start_manual()

    def zoom_press(self, direction):
        self.zoom_held = direction
        self.worker.manual_active = True
        self.cam.zoom(direction * self._level()["zoom"])

    def zoom_release(self):
        self.zoom_held = 0
        self.cam.zoom(0)
        self.worker.manual_active = bool(self.held) or self.ramp.active

    def emergency_stop(self):
        self.worker.cancel_home()
        self.set_tracking(False)
        self.held.clear()
        self.zoom_held = 0
        self._manual_reset()  # no ramp: the stop below is immediate
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
        self.tabs.addTab(self._shortcuts_tab(), "Shortcuts")
        self.tabs.addTab(self._picture_tab(), "Picture")
        self.tabs.addTab(self._about_tab(), "About")
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
        logs = nofocus(QPushButton("Open log folder"))
        logs.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(platform_paths.log_dir())))
        logs.setFixedWidth(160)
        v.addWidget(setting("Logs", "app.log has the details if the video or camera control isn't working", logs))
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
        spin.valueChanged.connect(self._stage_preset_changed)
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

    # ---------------- shortcuts tab
    def _shortcuts_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(4, 16, 4, 4)
        v.setSpacing(6)
        v.addWidget(label("Click a key, then press the new one. Esc cancels, Backspace clears. Pressing a key that is already used moves it here.", "hint", wrap=True))
        self.key_rows = {}
        groups = (("Held while pressed", keys.HELD), ("Pressed once", keys.PRESS))
        for title, kind in groups:
            v.addSpacing(8)
            v.addWidget(label(title, "section"))
            for action, text, k, _ in keys.ACTIONS:
                if k != kind or action.startswith("preset_"):
                    continue
                v.addLayout(self._key_row(action, text))
        v.addSpacing(8)
        v.addWidget(label("Presets: recall with these keys, hold Shift to store. B switches between slots 1–10 and 11–20.", "hint", wrap=True))
        grid = QGridLayout()
        grid.setSpacing(6)
        for i in range(1, 11):
            col = QVBoxLayout()
            col.setSpacing(2)
            col.addLayout(self._key_row(f"preset_{i}", f"Preset {i}", compact=True))
            grid.addLayout(col, (i - 1) // 5, (i - 1) % 5)
        v.addLayout(grid)
        restore = QPushButton("Restore default keys")
        restore.setObjectName("link")
        restore.clicked.connect(self._restore_keys)
        v.addWidget(restore, 0, Qt.AlignLeft)

        v.addSpacing(12)
        v.addWidget(label("Speed levels", "section"))
        v.addWidget(label("Z and C step through these. Pan/tilt and zoom speed for each level, 0–1 of the camera's top speed.", "hint", wrap=True))
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(4)
        grid.addWidget(label("Pan / tilt", "muted"), 0, 1)
        grid.addWidget(label("Zoom", "muted"), 0, 2)
        for i, name in enumerate(motion.LEVEL_NAMES):
            grid.addWidget(label(f"{i + 1}  {name}"), i + 1, 0)
            for col, field in ((1, "pan"), (2, "zoom")):
                grid.addWidget(slider(0.01, 1.0, self.levels[i][field], lambda x, i=i, f=field: self.levels[i].__setitem__(f, x)), i + 1, col)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        v.addLayout(grid)
        self.zoom_scale_check = QCheckBox("Slow down pan/tilt when zoomed in")
        self.zoom_scale_check.setChecked(self.cfg["zoom_scaled_speed"])
        self.zoom_scale_check.toggled.connect(lambda on: self.cfg.__setitem__("zoom_scaled_speed", on))
        v.addWidget(setting("Zoom-aware speed", "Keeps the picture moving at about the same rate at full zoom. Needs the Camera IP so the app can read the zoom.", self.zoom_scale_check))
        v.addStretch(1)
        self._refresh_key_rows()
        scroll = QScrollArea()
        scroll.setWidget(w)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        return scroll

    def _key_row(self, action, text, compact=False):
        row = QHBoxLayout()
        row.setSpacing(6)
        name = label(text if not compact else text.replace("Preset ", ""))
        name.setFixedWidth(170 if not compact else 18)
        row.addWidget(name)
        btns = []
        for slot in range(keys.SLOTS if not compact else 1):
            b = KeyButton()
            b.captured.connect(lambda code, a=action, sl=slot: self._bind_key(a, sl, code))
            b.cleared.connect(lambda a=action, sl=slot: self._clear_key(a, sl))
            row.addWidget(b)
            btns.append(b)
        note = label("")
        note.setStyleSheet(f"color: {AMBER}; font-size: 11px;")
        reset = QPushButton("Reset")
        reset.setObjectName("link")
        reset.clicked.connect(lambda _=False, a=action: self._reset_key(a))
        if not compact:
            row.addWidget(reset)
            row.addWidget(note, 1)
        else:
            reset.hide()
            note.hide()
        self.key_rows[action] = (btns, note)
        return row

    def _refresh_key_rows(self, notes=None):
        for action, (btns, note) in self.key_rows.items():
            for slot, b in enumerate(btns):
                code = self.binds.keys[action][slot]
                b.set_key(keys.pretty(code) if code else "")
            note.setText((notes or {}).get(action, ""))

    def _bind_key(self, action, slot, code):
        lost = self.binds.assign(action, slot, code)
        self._binds_changed({lost: f"Key moved to {keys.LABELS[action]}"} if lost else None)

    def _clear_key(self, action, slot):
        self.binds.clear(action, slot)
        self._binds_changed()

    def _reset_key(self, action):
        lost = self.binds.reset(action)
        self._binds_changed({a: f"Key moved to {keys.LABELS[action]}" for a in lost})

    def _restore_keys(self):
        self.binds.reset_all()
        self._binds_changed()

    # ---------------- picture tab
    def _picture_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(4, 16, 4, 4)
        v.setSpacing(10)
        self.pic_note = label("", "hint", wrap=True)
        v.addWidget(self.pic_note)
        self.pic_rows = {}
        self.pic_modes = {}

        def seg_row(title, field, options):
            row = QHBoxLayout()
            row.setSpacing(4)
            lbl = label(title, "section")
            lbl.setFixedWidth(100)
            row.addWidget(lbl)
            btns = {}
            for mode, text in options:
                b = nofocus(QPushButton(text))
                b.setObjectName("seg")
                b.setCheckable(True)
                b.setMinimumWidth(56)
                b.clicked.connect(lambda _=False, m=mode: self._pic_mode(field, m))
                row.addWidget(b)
                btns[mode] = b
            row.addStretch(1)
            self.pic_modes[field] = btns
            return row

        def pic_slider(field, title, fmt=None):
            r = slider(0, 1, self.pic[field], lambda x, f=field: self._pic_changed(f, x), 100.0, fmt)
            self.pic_rows[field] = r
            line = QHBoxLayout()
            lbl = label(title)
            lbl.setFixedWidth(100)
            line.addSpacing(16)
            line.addWidget(lbl)
            line.addWidget(r, 1)
            r.title = lbl
            return line

        v.addLayout(seg_row("Exposure", "exposure", [("auto", "Auto"), ("manual", "Manual")]))
        v.addLayout(pic_slider("brightness", "Brightness", picture.brightness_text))
        v.addLayout(pic_slider("iris", "Iris", picture.iris_text))
        v.addLayout(pic_slider("gain", "Gain", picture.gain_text))
        v.addLayout(pic_slider("shutter", "Shutter", picture.shutter_text))
        v.addSpacing(6)
        v.addLayout(seg_row("White balance", "wb", [("auto", "Auto"), ("indoor", "Indoor"), ("outdoor", "Outdoor"), ("once", "Once"), ("manual", "Manual")]))
        v.addLayout(pic_slider("red", "Red", picture.percent_text))
        v.addLayout(pic_slider("blue", "Blue", picture.percent_text))
        v.addSpacing(6)
        self.profile_box = QWidget()
        pb = QVBoxLayout(self.profile_box)
        pb.setContentsMargins(0, 0, 0, 0)
        pb.setSpacing(10)
        for field in ("contrast", "sharpness", "saturation"):
            pb.addLayout(pic_slider(field, field.capitalize(), picture.percent_text))
        v.addWidget(self.profile_box)
        self.profile_note = label("", "hint", wrap=True)
        v.addWidget(self.profile_note)

        v.addSpacing(6)
        looks = QHBoxLayout()
        looks.addWidget(label("Looks", "section"))
        self.looks_combo = QComboBox()
        self.looks_combo.setMinimumWidth(180)
        self.looks_combo.activated.connect(self._look_chosen)
        save = QPushButton("Save look…")
        save.clicked.connect(self._look_save)
        delete = QPushButton("Delete")
        delete.clicked.connect(self._look_delete)
        looks.addWidget(self.looks_combo, 1)
        looks.addWidget(save)
        looks.addWidget(delete)
        v.addLayout(looks)
        self.pic_reset = QPushButton("Reset camera to auto")
        self.pic_reset.clicked.connect(self._pic_reset)
        v.addWidget(self.pic_reset, 0, Qt.AlignLeft)
        v.addWidget(label("Values start from what PTZ Tracker last sent this camera. NDI and VISCA can't read the current picture back.", "hint", wrap=True))
        v.addStretch(1)
        self._look_refresh()
        self._picture_refresh()
        scroll = QScrollArea()
        scroll.setWidget(w)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        return scroll

    def _caps(self):
        return getattr(self.cam, "caps", frozenset())

    def _canon_items(self):
        return self.canon.items if self.canon else {}

    def _pic_key_name(self, ip=None):
        ip = self.ip_edit.text().strip() if ip is None else ip
        return ip or (self.source.name if self.source else "") or "default"

    def _picture_connected(self, ip):
        """New connection: load this camera's saved picture values and start the Canon HTTP probe."""
        self.pic_key = self._pic_key_name(ip)
        self.pic = picture.clean(self.cfg["picture"].get(self.pic_key))
        self.canon = picture.CanonPicture(ip, self.canon_probed.emit) if ip else None
        self._picture_refresh()

    def _canon_ready(self, items):
        self._picture_refresh()

    def _picture_refresh(self):
        """Puts the stored values into the controls and greys out what this camera/backend can't do."""
        if not hasattr(self, "pic_rows"):
            return
        caps, items = self._caps(), self._canon_items()
        p = self.pic
        for field, row in self.pic_rows.items():
            row.set(p[field])
        for field, btns in self.pic_modes.items():
            for mode, b in btns.items():
                b.setChecked(p[field] == mode)
        have_exp, have_wb = "exposure" in caps, "wb" in caps
        manual_exp, manual_wb = p["exposure"] == "manual", p["wb"] == "manual"
        for f, on in (("brightness", have_exp and not manual_exp), ("iris", have_exp and manual_exp), ("gain", have_exp and manual_exp),
                      ("shutter", have_exp and manual_exp), ("red", have_wb and manual_wb), ("blue", have_wb and manual_wb)):  # fmt: skip
            self.pic_rows[f].setEnabled(on)
            self.pic_rows[f].title.setEnabled(on)
        for b in self.pic_modes["exposure"].values():
            b.setEnabled(have_exp)
        for b in self.pic_modes["wb"].values():
            b.setEnabled(have_wb)
        self.pic_reset.setEnabled(have_exp or have_wb)
        avail = {w: (w == "sharpness" and "aperture" in caps) or w in items for w in picture.HTTP_WORDS}
        for w in picture.HTTP_WORDS:
            self.pic_rows[w].title.setVisible(avail[w])
            self.pic_rows[w].setVisible(avail[w])
        self.profile_box.setVisible(any(avail.values()))
        if not caps:
            self.pic_note.setText("Connect to the camera to change its picture.")
            self.profile_note.setText("")
        else:
            self.pic_note.setText(f"Changes are sent live over {self.cam.name}. Watch the program feed while you drag.")
            if not all(avail.values()):
                missing = [w.capitalize() for w in picture.HTTP_WORDS if not avail[w]]
                where = "over NDI" if self.cam.name == "NDI PTZ" else "on this camera"
                if self.canon is None:
                    extra = " Fill in the Camera IP and reconnect to look for them on the camera itself."
                else:
                    extra = " The camera's full item list was written to the log (Settings → Camera → Open log folder)."
                self.profile_note.setText(f"{', '.join(missing)}: not available {where}.{extra}")
            else:
                self.profile_note.setText("")

    def _pic_changed(self, field, value):
        self.pic[field] = value
        self.pic_dirty.add(field)
        self._pic_store()
        if not self.pic_timer.isActive():
            self.pic_timer.start()

    def _pic_store(self):
        if self.pic_key:
            self.cfg["picture"][self.pic_key] = dict(self.pic)

    def _pic_flush(self):
        """Sends what changed since the last flush: the newest value of each, at most every 100 ms."""
        dirty, self.pic_dirty = self.pic_dirty, set()
        caps = self._caps()
        groups = {picture.FIELD_GROUP[f] for f in dirty if f in picture.FIELD_GROUP}
        for g in groups:
            if picture.GROUPS[g][0] in caps:
                picture.send_group(self.cam, g, self.pic)
        self._send_http_picture(dirty)

    def _send_http_picture(self, fields):
        """Contrast / saturation (and sharpness when VISCA can't) go to the camera's HTTP API, if it has them."""
        items = self._canon_items()
        for f in fields:
            if f in picture.HTTP_WORDS and f in items and not (f == "sharpness" and "aperture" in self._caps()):
                self.canon.set(f, self.pic[f])

    def _pic_mode(self, field, mode):
        self.pic[field] = mode
        self._pic_store()
        if field == "exposure":
            self.cam.exposure_mode(mode)
            picture.send_group(self.cam, "exposure_fine" if mode == "manual" else "exposure_level", self.pic)
        else:
            self.cam.white_balance_mode(mode)
            if mode == "manual":
                picture.send_group(self.cam, "wb_rgb", self.pic)
        self._picture_refresh()

    def _pic_reset(self):
        self.pic = picture.reset_to_auto(self.pic)
        self._pic_store()
        self.cam.exposure_mode("auto")
        self.cam.white_balance_mode("auto")
        self._picture_refresh()
        self.statusBar().showMessage("Camera exposure and white balance set to auto")

    def _look_refresh(self, select=""):
        self.looks_combo.clear()
        self.looks_combo.addItem("Choose a look…")
        self.looks_combo.addItems(sorted(self.cfg["looks"]))
        if select:
            self.looks_combo.setCurrentText(select)

    def _look_chosen(self, index):
        name = self.looks_combo.itemText(index)
        if index <= 0 or name not in self.cfg["looks"]:
            return
        self.pic = picture.clean(self.cfg["looks"][name])
        self._pic_store()
        picture.send_all(self.cam, self.pic)
        self._send_http_picture(picture.HTTP_WORDS)
        self._picture_refresh()
        self.statusBar().showMessage(f"Picture look: {name}")

    def _look_save(self):
        name, ok = QInputDialog.getText(self.settings_dialog, "Save look", "Name for this look (e.g. Sunday morning)")
        name = name.strip()[:40]
        if ok and name:
            self.cfg["looks"][name] = dict(self.pic)
            self._look_refresh(name)
            self._save()

    def _look_delete(self):
        name = self.looks_combo.currentText()
        if self.looks_combo.currentIndex() > 0 and self.cfg["looks"].pop(name, None) is not None:
            self._look_refresh()
            self._save()

    # ---------------- updates
    def _update_bar(self):
        bar = QFrame()
        bar.setObjectName("updatebar")
        bar.hide()
        h = QHBoxLayout(bar)
        h.setContentsMargins(16, 6, 16, 6)
        self.update_label = label("")
        h.addWidget(self.update_label)
        self.update_progress_bar = QProgressBar()
        self.update_progress_bar.setTextVisible(False)
        self.update_progress_bar.setFixedWidth(160)
        self.update_progress_bar.hide()
        h.addWidget(self.update_progress_bar)
        h.addStretch(1)
        self.update_btns = []
        for text, fn, name in (("What's new", self.show_update_notes, "link"), ("Update now", self.update_now, "chip"), ("Later", self.update_later, "link")):
            b = nofocus(QPushButton(text))
            b.setObjectName(name)
            b.clicked.connect(fn)
            h.addWidget(b)
            self.update_btns.append(b)
        self.update_bar = bar
        return bar

    def _about_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(4, 16, 4, 4)
        v.setSpacing(16)
        v.addWidget(label(f"PTZ Tracker {__version__}", "cardTitle"))
        self.about_status = label("", "muted", wrap=True)
        check = QPushButton("Check for updates")
        check.setFixedWidth(160)
        check.clicked.connect(lambda: self.check_updates(manual=True))
        box = QWidget()
        bl = QVBoxLayout(box)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(check, 0, Qt.AlignLeft)
        bl.addWidget(self.about_status)
        v.addWidget(setting("Updates", "Updates download in the background and never install while you are tracking", box))
        auto = QCheckBox("Check automatically")
        auto.setChecked(self.cfg["update_auto"])
        auto.toggled.connect(lambda on: self.cfg.__setitem__("update_auto", on))
        v.addWidget(setting("Automatic checks", "A few seconds after launch and every 6 hours. Does nothing if the network is offline", auto))
        page = QPushButton("Open the releases page")
        page.setFixedWidth(180)
        page.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(updater.RELEASES_PAGE)))
        v.addWidget(setting("Releases", "Every version, with notes", page))
        v.addStretch(1)
        return w

    def _auto_check(self):
        if self.cfg["update_auto"]:
            self.check_updates()

    def check_updates(self, manual=False):
        if self._checking or self._updating:
            return
        self._checking = True
        if manual:
            self.about_status.setText("Checking…")

        def go():
            try:
                rel, err = updater.fetch_latest(), ""
                err = "" if rel else "the update file wasn't in the expected format"
            except Exception as e:
                rel, err = None, str(e) or type(e).__name__
            self.update_checked.emit(rel, err, manual)

        threading.Thread(target=go, daemon=True).start()

    def _update_checked(self, rel, err, manual):
        self._checking = False
        if rel is None:
            log.info("update check failed: %s", err)
            if manual:
                self.about_status.setText(f"Couldn't check for updates: {err}")
            return
        if not updater.is_newer(rel.version, __version__):
            if manual:
                self.about_status.setText(f"You're up to date (version {__version__}).")
            return
        self.release = rel
        if manual:
            self.about_status.setText(f"Version {rel.version} is available.")
        if manual or rel.version != self.cfg["update_skip"]:
            self.update_label.setText(f"PTZ Tracker {rel.version} is available")
            self.update_bar.show()

    def show_update_notes(self):
        if self.release:
            QMessageBox.information(self, f"PTZ Tracker {self.release.version}", self.release.notes or "No release notes.")

    def update_later(self):
        if self.release:
            self.cfg["update_skip"] = self.release.version
            self._save()
        self.update_bar.hide()

    def update_now(self):
        rel = self.release
        if rel is None or self._updating:
            return
        if self.track_btn.isChecked():
            if QMessageBox.question(self, "Stop tracking?", "Tracking is on. Stop tracking to update now?") != QMessageBox.Yes:
                return
            self.set_tracking(False)
        name, sha = updater.pick_asset(rel)
        if not updater.can_self_update() or not name:
            self._update_fallback(rel.page, "This copy can't update itself here." if getattr(sys, "frozen", False) else "")
            return
        self._updating = True
        for b in self.update_btns:
            b.setEnabled(False)
        self.update_label.setText(f"Downloading PTZ Tracker {rel.version}…")
        self.update_progress_bar.setValue(0)
        self.update_progress_bar.show()
        dest = os.path.join(updater.work_dir(), name)

        def go():
            try:
                updater.download(rel.url(name), dest, lambda f: self.update_progress.emit(int(f * 100)))
                if not updater.verify_sha256(dest, sha):
                    raise RuntimeError("the download did not match its checksum")
                self.update_ready.emit(dest, "")
            except Exception as e:
                self.update_ready.emit(dest if os.path.exists(dest) else "", str(e) or type(e).__name__)

        threading.Thread(target=go, daemon=True).start()

    def _update_progress(self, pct):
        self.update_progress_bar.setValue(pct)

    def _update_ready(self, path, err):
        try:
            if err:
                raise RuntimeError(err)
            self.update_label.setText("Installing the update…")
            if platform_paths.IS_WIN:
                updater.apply_windows(path)
            else:
                updater.apply_mac(path)
            log.info("update handed over, quitting")
            QTimer.singleShot(300, self._quit_for_update)
        except Exception as e:
            log.exception("update failed")
            self._updating = False
            self.update_progress_bar.hide()
            for b in self.update_btns:
                b.setEnabled(True)
            self.update_label.setText(f"PTZ Tracker {self.release.version} is available")
            self._update_fallback(path or self.release.page, f"The update couldn't be installed automatically ({e}).")

    def _update_fallback(self, target, message):
        """Open the downloaded file (or the release page) so the update can be done by hand."""
        if message:
            QMessageBox.warning(self, "Update", message + "\n\nIt will open now so you can finish by hand.")
        if target and os.path.exists(target):
            QDesktopServices.openUrl(QUrl.fromLocalFile(target))
        else:
            QDesktopServices.openUrl(QUrl(target or updater.RELEASES_PAGE))

    def _quit_for_update(self):
        self.close()
        QApplication.quit()

    # ---------------- keyboard
    def _press_actions(self):
        return {
            "track": lambda: self.set_tracking(not self.track_btn.isChecked()),
            "release": self.worker.release_target,
            "stop": self.emergency_stop,
            "home": self.camera_home,
            "stage_shot": self.go_stage_home,
            "focus_once": lambda: self.cam.one_push_af(),
            "speed_down": lambda: self.set_speed_level(self.speed_level - 1),
            "speed_up": lambda: self.set_speed_level(self.speed_level + 1),
            "bank": self.toggle_bank,
        }

    def eventFilter(self, obj, e):
        if e.type() not in (QEvent.KeyPress, QEvent.KeyRelease) or not self.isActiveWindow():
            return False
        if isinstance(QApplication.focusWidget(), (QLineEdit, QSpinBox, QComboBox)):
            return False
        press = e.type() == QEvent.KeyPress
        if e.isAutoRepeat():
            return True
        k = e.key()
        if press and k == Qt.Key_Comma and e.modifiers() & Qt.ControlModifier:  # ⌘, on a Mac; not rebindable
            self.open_settings()
            return True
        action, store = self.binds.resolve(k, bool(e.modifiers() & Qt.ShiftModifier))
        if action is None:
            return False
        if action in keys.HELD_DIRS:
            if press:
                self.manual_press(f"key{k}", *keys.HELD_DIRS[action])
            else:
                self.manual_release(f"key{k}")
        elif action in ("zoom_in", "zoom_out"):
            self.zoom_press(1 if action == "zoom_in" else -1) if press else self.zoom_release()
        elif action in ("focus_near", "focus_far"):
            self.cam.focus((-0.5 if action == "focus_near" else 0.5) if press else 0)
        elif not press:
            return False
        elif action.startswith("preset_"):
            self.preset(keys.preset_slot(action, self.bank), store=True if store else None)
        else:
            self._press_actions()[action]()
        return True

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
            keys=self.binds.to_cfg(),
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


_crash_file = None


def setup_logging():
    """Errors (including ones in background threads) go to app.log, since the Windows app has no console."""
    try:
        os.makedirs(os.path.dirname(APP_LOG), exist_ok=True)
        handler = logging.FileHandler(APP_LOG, encoding="utf-8")
    except OSError:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    sys.excepthook = lambda *exc: log.critical("uncaught error", exc_info=exc)
    try:  # a hard crash (inside Qt, NDI or the GPU driver) writes every thread's stack here
        global _crash_file
        _crash_file = open(os.path.join(os.path.dirname(APP_LOG), "crash.log"), "a", buffering=1)
        _crash_file.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} start\n")
        faulthandler.enable(_crash_file, all_threads=True)
    except OSError:
        pass
    threading.excepthook = lambda a: log.critical("uncaught error in %s", a.thread, exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


def windows_titlebar(widget):
    """Dark title bar in the app's own panel colour, so the window reads as one piece on Windows 10/11."""
    if not platform_paths.IS_WIN:
        return
    try:
        import ctypes

        hwnd = int(widget.winId())
        dwm = ctypes.windll.dwmapi
        on = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), 4)  # DWMWA_USE_IMMERSIVE_DARK_MODE
        c = QColor(PANEL)
        colorref = ctypes.c_int(c.red() | c.green() << 8 | c.blue() << 16)
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(colorref), 4)  # DWMWA_CAPTION_COLOR (Windows 11)
    except Exception:
        log.debug("title bar styling not available", exc_info=True)


def main():
    setup_logging()
    log.info("PTZ Tracker starting (%s, frozen=%s)", sys.platform, getattr(sys, "frozen", False))
    if platform_paths.IS_WIN:
        try:  # own taskbar entry with the app icon, not the generic Python one
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LWNRA.PTZTracker")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName("PTZ Tracker")
    app.setStyle("Fusion")
    app.setPalette(dark_palette())
    app.setStyleSheet(STYLE)
    icon = resource_path("assets", "icon.png")
    if os.path.exists(icon):
        app.setWindowIcon(QIcon(icon))
    if "--selftest" in sys.argv:
        from .selftest import run

        code = run(sys.argv[sys.argv.index("--selftest") + 1])
        logging.shutdown()
        os._exit(code)  # skip interpreter teardown: the test leaves NDI/ORT threads behind
    try:
        w = MainWindow()
    except Exception as e:
        log.exception("startup failed")
        QMessageBox.critical(None, "PTZ Tracker couldn't start", f"{e}\n\nDetails are in {APP_LOG}")
        sys.exit(1)
    windows_titlebar(w)
    windows_titlebar(w.settings_dialog)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
