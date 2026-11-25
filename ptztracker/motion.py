"""Manual-move maths: speed steps, ease in / out, diagonal and zoom-scaled speed. No Qt, so it is easy to test."""
import math

PT_STEPS, DEFAULT_PT_STEP = 16, 8  # pan/tilt speed steps, as in Canon's Remote Camera Control Application
PT_STEP1 = 0.004  # speed for step 1; the Canon API sends the camera's slowest speed for it, VISCA / NDI their floor
DEFAULT_ZOOM_SPEED = 0.5  # 50 %
EASE_IN, EASE_OUT = 0.25, 0.15  # seconds to reach full speed / to stop
MIN_SPEED = 0.03  # smallest pan/tilt worth sending: below this VISCA rounds to "stop"
MIN_ZOOM_SCALE = 0.15


def _num(v, default):
    return min(1.0, max(0.01, float(v))) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def pt_speed(step):
    """Pan/tilt step 1..16 -> speed 0..1. Step L is (L - 1) / 15, which the Canon API turns into RCCA's exact values."""
    return PT_STEP1 if step <= 1 else (min(step, PT_STEPS) - 1) / (PT_STEPS - 1)


def clean_step(v, default=DEFAULT_PT_STEP):
    return min(PT_STEPS, max(1, v)) if isinstance(v, int) and not isinstance(v, bool) else default


def nearest_step(speed):
    return min(range(1, PT_STEPS + 1), key=lambda L: abs(pt_speed(L) - speed))


def migrate_speed(cfg):
    """Fills in pt_step (1..16) and zoom_speed (0.01..1). Configs from before 1.5 had a 0..1 pan_speed (or a
    manual_speed); they get the nearest step. Their speed stops are dropped."""
    old = cfg.pop("pan_speed", cfg.pop("manual_speed", None))
    default = nearest_step(old) if isinstance(old, (int, float)) and not isinstance(old, bool) else DEFAULT_PT_STEP
    cfg["pt_step"] = clean_step(cfg.get("pt_step"), default)
    cfg["zoom_speed"] = _num(cfg.get("zoom_speed"), DEFAULT_ZOOM_SPEED)
    for k in ("speed_level", "speed_levels", "manual_speed"):
        cfg.pop(k, None)
    return cfg


def clean_speed(v):
    """A slider speed, kept inside 0.01..1."""
    return _num(v, 0.01)


def normalise(x, y):
    """Clip each axis to -1..1, and shrink a diagonal so it is no faster than a straight move."""
    x, y = max(-1.0, min(1.0, x)), max(-1.0, min(1.0, y))
    n = math.hypot(x, y)
    return (x / n, y / n) if n > 1.0 else (x, y)


def zoom_scale(hfov, widest):
    """Pan/tilt multiplier so the picture sweeps at about the same rate zoomed in as zoomed out."""
    if not hfov or not widest or widest <= 0:
        return 1.0
    return min(1.0, max(MIN_ZOOM_SCALE, hfov / widest))


def floor_speed(v):
    return 0.0 if v == 0 else math.copysign(max(abs(v), MIN_SPEED), v)


class Ramp:
    """Eases pan/tilt toward a target: ~EASE_IN s to full speed, ~EASE_OUT s back to zero (linear, per axis)."""

    def __init__(self):
        self.reset()

    @property
    def active(self):
        return any(self.cur) or any(self.target)

    def reset(self):
        self.cur, self.target, self.peak = [0.0, 0.0], [0.0, 0.0], [0.0, 0.0]

    def step(self, dt, pan, tilt):
        self.target = [pan, tilt]
        for i, t in enumerate(self.target):
            c = self.cur[i]
            self.peak[i] = max(self.peak[i], abs(t), abs(c))
            speeding_up = abs(t) > abs(c) and (c == 0 or (t > 0) == (c > 0))
            rate = abs(t) / EASE_IN if speeding_up else self.peak[i] / EASE_OUT  # units per second
            d = t - c
            c = t if abs(d) <= rate * dt else c + math.copysign(rate * dt, d)
            self.cur[i] = 0.0 if abs(c) < 1e-6 else c
            if self.cur[i] == 0 and t == 0:
                self.peak[i] = 0.0
        return self.cur[0], self.cur[1]
