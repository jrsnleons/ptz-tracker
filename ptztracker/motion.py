"""Manual-move maths: speed levels, ease in / out, diagonal and zoom-scaled speed. No Qt, so it is easy to test."""
import math

LEVEL_NAMES = ["Crawl", "Slow", "Medium", "Fast", "Max"]
DEFAULT_LEVELS = [
    {"pan": 0.03, "zoom": 0.15},
    {"pan": 0.08, "zoom": 0.25},
    {"pan": 0.18, "zoom": 0.40},
    {"pan": 0.35, "zoom": 0.60},
    {"pan": 0.70, "zoom": 1.00},
]
DEFAULT_LEVEL = 3
EASE_IN, EASE_OUT = 0.25, 0.15  # seconds to reach full speed / to stop
MIN_SPEED = 0.03  # smallest pan/tilt worth sending: below this VISCA rounds to "stop"
MIN_ZOOM_SCALE = 0.15


def _num(v, default):
    return min(1.0, max(0.01, float(v))) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def clean_levels(levels):
    """Saved speed levels, repaired: always 5 {pan, zoom} entries in 0.01..1."""
    out = []
    for i, d in enumerate(DEFAULT_LEVELS):
        src = levels[i] if isinstance(levels, list) and i < len(levels) and isinstance(levels[i], dict) else {}
        out.append({k: _num(src.get(k), d[k]) for k in d})
    return out


def nearest_level(speed, levels=DEFAULT_LEVELS):
    """1-based level whose pan speed is closest to an old single 'manual speed' value."""
    return 1 + min(range(len(levels)), key=lambda i: abs(levels[i]["pan"] - speed))


def migrate_speed(cfg):
    """Old configs had one manual_speed slider; pick the nearest level. Fills in speed_level / speed_levels."""
    cfg["speed_levels"] = clean_levels(cfg.get("speed_levels"))
    level = cfg.get("speed_level")
    if not isinstance(level, int) or isinstance(level, bool):
        old = cfg.get("manual_speed")
        level = nearest_level(float(old), cfg["speed_levels"]) if isinstance(old, (int, float)) else DEFAULT_LEVEL
    cfg["speed_level"] = min(max(level, 1), len(LEVEL_NAMES))
    cfg.pop("manual_speed", None)
    return cfg


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
