"""Target selection + PID camera controller (same approach as the Windows tracker)."""
import time
from dataclasses import dataclass

import numpy as np


def iou(box, boxes):
    x1 = np.maximum(box[0], boxes[:, 0])
    y1 = np.maximum(box[1], boxes[:, 1])
    x2 = np.minimum(box[2], boxes[:, 2])
    y2 = np.minimum(box[3], boxes[:, 3])
    inter = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
    a = (box[2] - box[0]) * (box[3] - box[1])
    b = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    return inter / np.maximum(a + b - inter, 1e-6)


class TargetTracker:
    """Follows one head across frames by matching detections to the last known box."""

    def __init__(self, max_lost=45):
        self.box = None  # [x1, y1, x2, y2]
        self.velocity = np.zeros(4)
        self.lost = 0
        self.max_lost = max_lost

    @property
    def active(self):
        return self.box is not None

    @property
    def is_lost(self):
        return self.box is not None and self.lost > 0

    def select_at(self, x, y, dets):
        for d in dets:
            if d[0] <= x <= d[2] and d[1] <= y <= d[3]:
                self.box = d[:4].copy()
                self.velocity[:] = 0
                self.lost = 0
                return True
        return False

    def select_largest(self, dets):
        if len(dets) == 0:
            return False
        areas = (dets[:, 2] - dets[:, 0]) * (dets[:, 3] - dets[:, 1])
        self.box = dets[int(np.argmax(areas)), :4].copy()
        self.velocity[:] = 0
        self.lost = 0
        return True

    def release(self):
        self.box = None
        self.lost = 0

    def update(self, dets):
        if self.box is None:
            return None
        predicted = self.box + self.velocity
        if len(dets):
            ious = iou(predicted, dets)
            # fall back to centre distance when the head moved more than its own size
            w = predicted[2] - predicted[0]
            pc = np.array([(predicted[0] + predicted[2]) / 2, (predicted[1] + predicted[3]) / 2])
            dc = np.stack([(dets[:, 0] + dets[:, 2]) / 2, (dets[:, 1] + dets[:, 3]) / 2], axis=1)
            dist = np.linalg.norm(dc - pc, axis=1) / max(w, 1)
            best = int(np.argmax(ious)) if ious.max() > 0.2 else int(np.argmin(dist))
            if ious[best] > 0.2 or dist[best] < 1.5:
                new = dets[best, :4]
                self.velocity = 0.6 * self.velocity + 0.4 * (new - self.box)
                self.box = new.copy()
                self.lost = 0
                return self.box
        self.lost += 1
        self.velocity *= 0.8
        if self.lost > self.max_lost:
            self.release()
        return None


class PID:
    def __init__(self, kp, ki, kd):
        self.kp, self.ki, self.kd = kp, ki, kd
        self.integral = 0.0
        self.prev = None
        self.t = None

    def reset(self):
        self.integral = 0.0
        self.prev = None
        self.t = None

    def __call__(self, error):
        now = time.monotonic()
        dt = 0.033 if self.t is None else max(now - self.t, 1e-3)
        self.t = now
        self.integral = float(np.clip(self.integral + error * dt, -1, 1))
        deriv = 0.0 if self.prev is None else (error - self.prev) / dt
        self.prev = error
        return self.kp * error + self.ki * self.integral + self.kd * deriv


@dataclass
class TrackSettings:
    speed: float = 1.0  # overall gain (config "speed" from the Windows build is ~2.2 on its scale)
    max_speed: float = 0.6  # 0..1 of the camera's max speed
    deadzone_x: float = 0.06  # fraction of frame width either side of the target point
    deadzone_y: float = 0.08
    exponent: float = 1.4  # >1 = gentle near target, faster when far away
    target_x: float = 0.5  # where the head should sit in frame (0..1)
    target_y: float = 0.33
    target_height: float = 0.14  # desired head height as fraction of frame (for auto zoom)
    lock_horizontal: bool = False
    lock_vertical: bool = True
    lock_zoom: bool = True
    smoothing: float = 0.35  # 0..1 output low-pass (higher = snappier)
    ignore_below: float = 0.85  # heads whose centre is below this line (audience) are ignored


class CamController:
    def __init__(self, settings: TrackSettings):
        self.s = settings
        self.pid_x = PID(1.0, 0.05, 0.05)
        self.pid_y = PID(1.0, 0.05, 0.05)
        self.moving_x = False
        self.moving_y = False
        self.out = np.zeros(3)  # pan, tilt, zoom

    def reset(self):
        self.pid_x.reset()
        self.pid_y.reset()
        self.moving_x = self.moving_y = False
        self.out[:] = 0

    def _axis(self, err, dz, moving, pid):
        # hysteresis: start moving outside the deadzone, stop once well inside it
        if moving:
            moving = abs(err) > dz * 0.4
        else:
            moving = abs(err) > dz
        if not moving:
            pid.reset()
            return 0.0, moving
        shaped = np.sign(err) * (min(abs(err), 1.0) ** self.s.exponent)
        v = pid(shaped) * self.s.speed
        return float(np.clip(v, -self.s.max_speed, self.s.max_speed)), moving

    def compute(self, box, frame_w, frame_h):
        """box in frame pixels (or None when the target is lost). Returns pan, tilt, zoom."""
        s = self.s
        target = np.zeros(3)
        if box is not None:
            cx = (box[0] + box[2]) / 2 / frame_w
            cy = (box[1] + box[3]) / 2 / frame_h
            hh = (box[3] - box[1]) / frame_h
            ex = (cx - s.target_x) * 2  # -1..1
            ey = (s.target_y - cy) * 2  # + = head above target -> tilt up
            if not s.lock_horizontal:
                target[0], self.moving_x = self._axis(ex, s.deadzone_x * 2, self.moving_x, self.pid_x)
            if not s.lock_vertical:
                target[1], self.moving_y = self._axis(ey, s.deadzone_y * 2, self.moving_y, self.pid_y)
            if not s.lock_zoom and hh > 0:
                ratio = s.target_height / hh  # >1 = head too small -> zoom in
                ez = np.log(ratio)
                if abs(ez) > 0.25:
                    target[2] = float(np.clip(ez * 0.5, -0.5, 0.5))
        else:
            self.reset()
        # ease into motion, but stop right away once an axis is inside its deadzone
        self.out = self.out + (target - self.out) * s.smoothing
        self.out[target == 0] = 0.0
        self.out[np.abs(self.out) < 0.02] = 0.0
        return tuple(float(v) for v in self.out)
