"""Canon WV-HTTP: the camera's own web API, the same one Canon's Remote Camera Control Application (RCCA) uses.

One CanonClient per camera owns everything that goes over it:
- a session (GET /-wvhttp-01-/open, then s=<id> on every request), logging in when a user name is set. It opens on
  the first command, so reading the position alone (which works without one) never takes control from RCCA;
- one poll thread: the position (c.1.pan / tilt / zoom, 1/100 degree) every `interval`, and the whole info list
  (presets, picture values and their .min / .max / .list ranges) every FULL_EVERY seconds;
- one writer thread: commands queue by group, and only the newest command per group is sent, so a burst of slider
  moves can't arrive out of order and a held key never builds a backlog.

CanonAPI is the PTZBase backend on top of it. Speeds follow RCCA (see pt_wire / zoom_wire).
Untested on the real CR-N300: the session flow, auth type and preset/picture keys come from reading RCCA 1.7.1.
"""
import collections
import logging
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .motion import PT_STEP1
from .ptz import PTZBase
from .stage import CamPos

log = logging.getLogger(__name__)

PT_MAX = 5000  # RCCA caps manual pan/tilt at this wire speed


def parse(text):
    """key=value lines -> dict. Canon writes `key:=value`; RCCA also accepts `==` and `=`."""
    out = {}
    for line in text.splitlines():
        for sep in (":=", "==", "="):
            if sep in line:
                k, v = line.split(sep, 1)
                out[k.strip()] = v.strip()
                break
    return out


def num(d, key, default):
    try:
        return int(float(d[key]))
    except (KeyError, TypeError, ValueError):
        return default


def pt_wire(v, lo=10):
    """Pan/tilt speed float (0..1 = 0..PT_MAX) -> camera speed. RCCA's 16 steps are (step - 1) / 15 here, so step L
    sends floor(5000 * (L - 1) / 15) exactly as RCCA does, and step 1 sends the camera's minimum."""
    v = abs(v)
    return lo if v <= PT_STEP1 else max(lo, math.floor(PT_MAX * min(v, 1.0) + 1e-9))


def zoom_wire(v, zmax=15, zmin=0):
    """Zoom speed float (0..1, the 0-100 % slider) -> camera zoom speed, as RCCA: floor(round(max * pct, 1))."""
    return max(zmin, math.floor(round(zmax * min(abs(v), 1.0), 1)))


class CanonClient:
    BASE = "-wvhttp-01-"
    FULL_EVERY = 2.0  # seconds between full info reads (presets, picture values)

    def __init__(self, host, user="", password="", interval=0.08, on_state=None):
        self.host, self.interval, self.on_state = host, interval, on_state
        handlers = []
        if user:
            mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            mgr.add_password(None, f"http://{host}/", user, password)
            handlers = [urllib.request.HTTPDigestAuthHandler(mgr), urllib.request.HTTPBasicAuthHandler(mgr)]
        self._opener = urllib.request.build_opener(*handlers)
        self.sid = None
        self.state = {}  # the camera's whole info list, as text
        self.history = collections.deque(maxlen=60)
        self.error = None
        self._lock = threading.Lock()
        self._pending = {}  # group -> (path, params); newest wins
        self._wake = threading.Event()
        self._run = True
        self._threads = [threading.Thread(target=f, daemon=True) for f in (self._poll_loop, self._write_loop)]
        for t in self._threads:
            t.start()

    # ---------------- HTTP
    def _get(self, path, params=None, timeout=2.0):
        q = dict(params or {})
        if self.sid and path != "open":
            q["s"] = self.sid
        url = f"http://{self.host}/{self.BASE}/{path}" + (f"?{urllib.parse.urlencode(q)}" if q else "")
        with self._opener.open(url, timeout=timeout) as r:
            return r.read().decode(errors="replace")

    def _open(self):
        try:
            d = parse(self._get("open", {"s.priority": 40}))
            self.sid = d.get("s") or None
            log.info("Canon session %s (control: %s)", "open" if self.sid else "refused", d.get("s.authority.control", "?"))
        except Exception as e:
            self.sid = None
            log.info("Canon session open failed, reading without one: %s", e)

    def _info(self, items=None):
        try:
            return parse(self._get("info", {"item": items} if items else None))
        except urllib.error.HTTPError as e:
            if e.code == 404:  # older firmware: only the .cgi name (what 1.4 used)
                return parse(self._get("info.cgi", {"item": items} if items else None))
            raise

    # ---------------- threads
    def _poll_loop(self):
        next_full = 0.0
        while self._run:
            try:
                now = time.monotonic()
                if now >= next_full:
                    full = self._info()
                    with self._lock:
                        self.state = full
                    next_full = now + self.FULL_EVERY
                    if self.on_state:
                        self.on_state(dict(full))
                d = self._info("c.1.pan,c.1.tilt,c.1.zoom")
                p = CamPos(int(d["c.1.pan"]) / 100, int(d["c.1.tilt"]) / 100, int(d["c.1.zoom"]) / 100, time.monotonic())
                with self._lock:
                    self.history.append(p)
                self.error = None
            except Exception as e:
                self.error = str(e) or type(e).__name__
                time.sleep(0.5)
            time.sleep(self.interval)

    def _write_loop(self):
        while self._run:
            self._wake.wait(0.5)
            with self._lock:
                self._wake.clear()
                batch, self._pending = list(self._pending.items()), {}
            if batch and not self.sid:
                self._open()
            for group, (path, params) in batch:
                try:
                    self._get(path, params)
                except Exception as e:
                    log.info("Canon %s %s failed: %s", path, params, e)
                    if isinstance(e, urllib.error.HTTPError) and e.code in (401, 403, 409, 410):
                        self._open()

    def send(self, group, params, path="control"):
        """Queues a command; a newer one in the same group replaces it if it hasn't gone out yet."""
        with self._lock:
            self._pending[group] = (path, params)
        self._wake.set()

    def close(self):
        self._run = False
        self._wake.set()
        sid, self.sid = self.sid, None
        if sid:
            threading.Thread(target=lambda: self._quiet_get("close", {"s": sid}), daemon=True).start()

    def _quiet_get(self, path, params):
        try:
            self._get(path, params, timeout=1.0)
        except Exception:
            pass

    # ---------------- reading
    def snapshot(self):
        with self._lock:
            return dict(self.state)

    def value(self, key, default=None):
        with self._lock:
            return self.state.get(key, default)

    def thumbnail(self, n):
        """JPEG bytes of preset n's thumbnail, or None. Blocking: call it off the GUI thread."""
        try:
            with self._opener.open(
                f"http://{self.host}/{self.BASE}/thumbnail?" + urllib.parse.urlencode({"p": n, "s": self.sid or ""}), timeout=3
            ) as r:
                return r.read()
        except Exception:
            return None

    # position history (same API the stage code used with CanonPositionPoller)
    def latest(self, max_age=1.0):
        with self._lock:
            p = self.history[-1] if self.history else None
        return p if p and time.monotonic() - p.t <= max_age else None

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
        return all(max(getattr(h, a) for h in recent) - min(getattr(h, a) for h in recent) < tol for a in ("pan", "tilt", "hfov"))

    def status(self):
        p = self.latest()
        if p is None:
            return f"position: unavailable ({self.error or 'waiting'})"
        return f"position: pan {p.pan:+.2f}°  tilt {p.tilt:+.2f}°  view {p.hfov:.1f}°"


class CanonAPI(PTZBase):
    """Camera control over the Canon web API: what RCCA sends, plus preset speeds and every picture setting."""

    name = "Canon API"
    caps = frozenset({"exposure", "wb", "aperture", "preset_speed", "zoom_linked"})

    def __init__(self, client: CanonClient, zoom_linked=lambda: True):
        self.c = client
        self.zoom_linked = zoom_linked  # () -> bool. RCCA's "Control by zoom position": the camera slows pan/tilt when zoomed in
        self._pt = None

    def _lo(self, axis):
        return num(self.c.snapshot(), f"c.1.{axis}.speed.min", 10)

    def pan_tilt(self, pan, tilt):
        q = {}
        mode = "auto2" if self.zoom_linked() else "manual"
        for axis, v, pos, neg in (("pan", pan, "right", "left"), ("tilt", tilt, "up", "down")):
            if v == 0:
                q[axis] = "stop"
            else:
                q.update({axis: pos if v > 0 else neg, f"{axis}.speed.dir": pt_wire(v, self._lo(axis)), f"{axis}.speed.mode.dir": mode})
        if q != self._pt:  # held keys and tracking repeat the same command many times a second
            self._pt = q
            self.c.send("pt", q)

    def zoom(self, speed):
        if speed == 0:
            self.c.send("zoom", {"zoom": "stop"})
            return
        d = self.c.snapshot()
        w = zoom_wire(speed, num(d, "c.1.zoom.speed.max", 15), num(d, "c.1.zoom.speed.min", 0))
        self.c.send("zoom", {"zoom": "tele" if speed > 0 else "wide", "zoom.speed": w})

    # focus / autofocus words are unconfirmed on the CR-N300 (RCCA's code only showed focus.action=one_shot)
    def focus(self, speed):
        self.c.send("focus", {"focus": "stop" if speed == 0 else ("far" if speed > 0 else "near")})

    def autofocus(self, on=True):
        self.c.send("focus", {"focus": "auto" if on else "manual"})

    def one_push_af(self):
        self.c.send("focus", {"focus.action": "one_shot"})

    def home(self):
        self.recall_preset(1)  # RCCA's Home is preset 1

    def stop(self):
        self._pt = None
        self.c.send("pt", {"pan": "stop", "tilt": "stop"})
        self.c.send("zoom", {"zoom": "stop"})

    def recall_preset(self, n, speed=None, time_ms=None):
        """speed / time_ms: camera units (p.ptzspeed.min..max / p.ptztime.min..max). Neither: the preset's saved speed."""
        q = {"p": n}
        if speed is not None:
            q["p.ptzspeed"] = int(speed)
        elif time_ms is not None:
            q["p.ptztime"] = int(time_ms)
        elif "on" in self.c.value("p.ptzspeed.saved.list", ""):
            q["p.ptzspeed.saved"] = "on"
        self.c.send("preset", q)

    def store_preset(self, n, name=None, speed=None):
        q = {"p": n, "ptz": "enabled", "focus": "enabled", "exp": "enabled", "wb": "enabled", "thumbnail": "enabled"}
        q["name"] = name if name is not None else self.c.value(f"p.{n}.name.utf8", f"Preset {n}")
        if speed is not None:
            q["ptz.speed"] = int(speed)
        self.c.send(f"store{n}", q, path="preset/set")

    def set(self, key, value):
        """Any picture or camera setting by its control name (e.g. "me.brightness", "wb", "ac")."""
        self.c.send(f"set:{key}", {key: value})

    def save_settings(self):
        self.c.send("save", {"s.action": "save"})

    def close(self):
        self.stop()
        time.sleep(0.15)  # let the stop go out before the session closes
        self.c.close()

    def status(self):
        if self.c.error:
            return f"Canon API {self.c.host}: {self.c.error}"
        return f"Canon API {self.c.host}: {'session open' if self.c.sid else 'connecting…'}"
