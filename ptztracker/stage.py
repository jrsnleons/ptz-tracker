"""Stage boundary: camera position readback (Canon HTTP API) + pixel <-> angle geometry.

The CR-N300 reports its position at /-wvhttp-01-/info.cgi in 1/100 degree:
    c.1.pan  (+ = right), c.1.tilt (+ = up), c.1.zoom (horizontal field of view)
"""
import collections
import math
import threading
import time
import urllib.request
from dataclasses import dataclass


@dataclass
class CamPos:
    pan: float  # degrees, + = right
    tilt: float  # degrees, + = up
    hfov: float  # horizontal field of view, degrees
    t: float  # time.monotonic() of the reading


class CanonPositionPoller:
    """Polls the camera position on a background thread and keeps a short history,
    so a video frame (which arrives ~0.3 s late) can be matched with where the camera was."""

    def __init__(self, host, interval=0.08):
        self.url = f"http://{host}/-wvhttp-01-/info.cgi?item=c.1.pan,c.1.tilt,c.1.zoom"
        self.interval = interval
        self.history = collections.deque(maxlen=60)
        self.error = None
        self._lock = threading.Lock()
        self._run = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _read(self):
        text = urllib.request.urlopen(self.url, timeout=1.0).read().decode()
        d = dict(line.split(":=", 1) for line in text.splitlines() if ":=" in line)
        return CamPos(int(d["c.1.pan"]) / 100, int(d["c.1.tilt"]) / 100, int(d["c.1.zoom"]) / 100, time.monotonic())

    def _loop(self):
        while self._run:
            try:
                p = self._read()
                with self._lock:
                    self.history.append(p)
                self.error = None
            except Exception as e:
                self.error = str(e) or type(e).__name__
                time.sleep(0.5)
            time.sleep(self.interval)

    def latest(self, max_age=1.0):
        with self._lock:
            if not self.history:
                return None
            p = self.history[-1]
        return p if time.monotonic() - p.t <= max_age else None

    def at(self, t):
        """Reading closest to time t (for latency compensation)."""
        with self._lock:
            if not self.history:
                return None
            p = min(self.history, key=lambda h: abs(h.t - t))
        return p if abs(p.t - t) < 1.0 else None

    def settled(self, seconds=0.5, tol=0.05):
        """True when the camera hasn't moved for `seconds`."""
        now = time.monotonic()
        with self._lock:
            recent = [h for h in self.history if now - h.t <= seconds]
        if len(recent) < 3 or recent[0].t > now - seconds * 0.7:
            return False
        return (
            max(h.pan for h in recent) - min(h.pan for h in recent) < tol
            and max(h.tilt for h in recent) - min(h.tilt for h in recent) < tol
            and max(h.hfov for h in recent) - min(h.hfov for h in recent) < tol
        )

    def status(self):
        p = self.latest()
        if p is None:
            return f"position: unavailable ({self.error or 'waiting'})"
        return f"position: pan {p.pan:+.2f}°  tilt {p.tilt:+.2f}°  view {p.hfov:.1f}°"

    def close(self):
        self._run = False


# ---------------------------------------------------------------- geometry (pinhole camera)


def _half_tans(pos: CamPos, w, h):
    tx = math.tan(math.radians(pos.hfov) / 2)
    return tx, tx * h / w


def pixel_to_angle(x, y, pos: CamPos, w, h):
    tx, ty = _half_tans(pos, w, h)
    pan = pos.pan + math.degrees(math.atan((x / w - 0.5) * 2 * tx))
    tilt = pos.tilt - math.degrees(math.atan((y / h - 0.5) * 2 * ty))
    return pan, tilt


def angle_to_pixel(pan, tilt, pos: CamPos, w, h):
    tx, ty = _half_tans(pos, w, h)
    dp, dt = math.radians(pan - pos.pan), math.radians(tilt - pos.tilt)
    if abs(dp) >= math.pi / 2 or abs(dt) >= math.pi / 2:
        return None
    return (0.5 + math.tan(dp) / (2 * tx)) * w, (0.5 - math.tan(dt) / (2 * ty)) * h


@dataclass
class StageBounds:
    pan_min: float
    pan_max: float
    tilt_min: float
    tilt_max: float

    @classmethod
    def from_rect(cls, x1, y1, x2, y2, pos, w, h):
        a = pixel_to_angle(min(x1, x2), min(y1, y2), pos, w, h)
        b = pixel_to_angle(max(x1, x2), max(y1, y2), pos, w, h)
        return cls(a[0], b[0], b[1], a[1])

    def contains(self, pan, tilt, margin=0.0):
        return (
            self.pan_min - margin <= pan <= self.pan_max + margin
            and self.tilt_min - margin <= tilt <= self.tilt_max + margin
        )

    def outline(self, pos, w, h, steps=12):
        """Boundary as image points for the current camera position (edges sampled so they bend correctly)."""
        pts = []
        corners = [
            (self.pan_min, self.tilt_max), (self.pan_max, self.tilt_max),
            (self.pan_max, self.tilt_min), (self.pan_min, self.tilt_min),
        ]  # fmt: skip
        for (p0, t0), (p1, t1) in zip(corners, corners[1:] + corners[:1]):
            for i in range(steps):
                f = i / steps
                pt = angle_to_pixel(p0 + (p1 - p0) * f, t0 + (t1 - t0) * f, pos, w, h)
                if pt is not None:
                    pts.append(pt)
        return pts


@dataclass
class StageSettings:
    enabled: bool = False
    bounds: dict = None  # StageBounds as a dict, set by drawing on the video
    home_preset: int = 10  # wide stage shot; 10 keeps it clear of the 1-9 preset buttons
    leave_seconds: float = 2.0  # target outside the stage this long -> go home
    lost_seconds: float = 3.0  # nobody tracked this long -> go home
    auto_resume: bool = True  # pick someone up again once they're back on stage
    video_latency: float = 0.3  # video arrives this much later than the position readback
    margin: float = 0.5  # degrees of slack around the boundary
    home: dict = None  # {"pan", "tilt", "hfov"} of the stage shot, to check the camera really got there
    resume_move: bool = False  # when someone comes back, first move to resume_shot (position + zoom) before following
    resume_shot: dict = None  # {"pan", "tilt", "hfov"} to frame before following resumes
    resume_speed: float = 0.4  # fastest the camera moves on that trip, 0..1 of its top speed

    def get_bounds(self):
        return StageBounds(**self.bounds) if self.bounds else None


class Homing:
    """Takes the camera back to the stage shot.

    1. stop, and give the camera a moment (a recall sent in the same burst as the stops can be dropped)
    2. recall the preset, and watch the position readback until it arrives
    3. if the camera stops somewhere else, recall once more
    4. still not there: drive pan/tilt/zoom to the stored angles ourselves
    """

    SETTLE = 0.25
    PAN_TOL, TILT_TOL, FOV_TOL = 0.4, 0.4, 1.0

    def __init__(self, cam, poller, preset, home, drive_only=False, max_speed=0.5):
        self.cam, self.poller, self.preset = cam, poller, preset
        self.home = home  # dict or None (stage drawn before home angles were recorded)
        self.max_speed = max_speed
        self.phase = "drive" if drive_only else "stopping"
        self.t = time.monotonic()
        self.tries = 0
        self.result = None  # set when done: "preset", "preset, 2nd try", "driven there", "gave up"
        cam.stop()

    def _arrived(self, p):
        h = self.home
        return (
            abs(p.pan - h["pan"]) < self.PAN_TOL
            and abs(p.tilt - h["tilt"]) < self.TILT_TOL
            and abs(p.hfov - h["hfov"]) < self.FOV_TOL
        )

    def cancel(self):
        if self.phase == "drive":
            self.cam.stop()
        self.phase = "done"
        self.result = self.result or "cancelled"

    def step(self):
        """Call every frame. Returns True once finished."""
        now = time.monotonic()
        if self.phase == "done":
            return True
        pos = self.poller.latest() if self.poller else None

        if self.phase == "stopping":
            if now - self.t >= self.SETTLE:
                self.cam.recall_preset(self.preset)
                self.tries += 1
                self.phase, self.t = "recall", now
            return False

        if self.phase == "recall":
            if self.home is None or pos is None:
                # nothing to check against: trust the recall
                if now - self.t > 0.5:
                    self.phase, self.result = "done", "preset"
                return self.phase == "done"
            if self._arrived(pos):
                self.phase = "done"
                self.result = "preset" if self.tries == 1 else "preset, 2nd try"
                return True
            stuck = now - self.t > 1.2 and self.poller.settled(0.6)
            if stuck or now - self.t > 8.0:
                if self.tries < 2:
                    self.cam.stop()
                    self.phase, self.t = "stopping", now
                else:
                    self.phase, self.t = "drive", now
            return False

        if self.phase == "drive":
            if pos is None:
                return False
            if self._arrived(pos):
                self.cam.stop()
                self.phase, self.result = "done", "driven there"
                return True
            if now - self.t > 20.0:
                self.cam.stop()
                self.phase, self.result = "done", "gave up"
                return True
            h = self.home
            self.cam.pan_tilt(
                _drive(h["pan"] - pos.pan, self.PAN_TOL * 0.5, self.max_speed),
                _drive(h["tilt"] - pos.tilt, self.TILT_TOL * 0.5, self.max_speed),
            )
            ez = h["hfov"] - pos.hfov  # + = need a wider view
            if abs(ez) < self.FOV_TOL * 0.5:
                self.cam.zoom(0)
            else:
                self.cam.zoom((-1 if ez > 0 else 1) * min(self.max_speed, 0.4 if abs(ez) > 3 else 0.15))
            return False
        return True


def _drive(err, tol, max_speed=0.5):
    """Speed toward a target angle: proportional, with a floor so it doesn't creep forever."""
    if abs(err) < tol:
        return 0.0
    return math.copysign(min(max_speed, max(0.05, abs(err) * 0.06)), err)
