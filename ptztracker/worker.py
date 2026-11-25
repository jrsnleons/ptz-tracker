"""TrackerWorker: the video thread that detects, tracks, homes to the stage and sends camera speeds."""
import logging
import os
import threading
import time

import numpy as np
from PySide6.QtCore import QThread, Signal

from . import platform_paths
from . import ptz as ptzmod
from .stage import Homing, StageSettings, drive_zoom, pixel_to_angle
from .tracking import CamController, TargetTracker, TrackSettings

log = logging.getLogger("ptztracker")


PERF_LOG = os.path.join(platform_paths.log_dir(), "perf.log")
STALL_STOP = 0.5  # no new video for this long while tracking -> stop the camera


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
        return drive_zoom(err, self.stage.resume_speed)

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
