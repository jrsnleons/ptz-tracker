"""Simulated-camera tests for the return-to-stage-shot logic (no hardware needed).

Run:  .venv/bin/python -m tests.test_homing
"""
import threading
import time
from dataclasses import asdict

import numpy as np

from ptztracker.stage import CamPos, Homing, StageBounds, StageSettings


class SimCamera:
    """Integrates speed commands into a position, like a real PTZ head.

    recall_mode: "obey"      - presets always work
                 "busy_drop" - a recall that arrives <50 ms after another command is dropped
                 "ignore"    - presets never work (forces the closed-loop fallback)
    """

    def __init__(self, recall_mode="obey", pan=14.0, tilt=-2.0, hfov=30.0):
        self.mode = recall_mode
        self.pos = [pan, tilt, hfov]
        self.speed = [0.0, 0.0, 0.0]
        self.presets = {}
        self.target = None
        self.last_cmd = 0.0
        self.recalls_accepted = self.recalls_dropped = 0
        self.lock = threading.Lock()
        self._run = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        last = time.monotonic()
        while self._run:
            time.sleep(0.01)
            now = time.monotonic()
            dt, last = now - last, now
            with self.lock:
                if self.target is not None:
                    for i, rate in enumerate((40, 40, 30)):  # deg/s during preset moves
                        d = self.target[i] - self.pos[i]
                        self.pos[i] += max(-rate * dt, min(rate * dt, d))
                    if all(abs(self.target[i] - self.pos[i]) < 1e-3 for i in range(3)):
                        self.target = None
                else:
                    self.pos[0] += self.speed[0] * 60 * dt
                    self.pos[1] += self.speed[1] * 60 * dt
                    self.pos[2] = min(63.4, max(3.4, self.pos[2] - self.speed[2] * 30 * dt))

    def _cmd(self):
        self.last_cmd = time.monotonic()

    def pan_tilt(self, pan, tilt):
        with self.lock:
            if pan or tilt:
                self.target = None
            self.speed[0], self.speed[1] = pan, tilt
            self._cmd()

    def zoom(self, z):
        with self.lock:
            if z:
                self.target = None
            self.speed[2] = z
            self._cmd()

    def stop(self):
        self.pan_tilt(0, 0)
        self.zoom(0)

    def store_preset(self, n):
        with self.lock:
            self.presets[n] = list(self.pos)

    def recall_preset(self, n, speed=1.0):
        with self.lock:
            too_soon = time.monotonic() - self.last_cmd < 0.05
            if self.mode == "ignore" or (self.mode == "busy_drop" and too_soon):
                self.recalls_dropped += 1
            else:
                self.recalls_accepted += 1
                self.speed = [0.0, 0.0, 0.0]
                self.target = list(self.presets[n])
            self._cmd()

    def close(self):
        self._run = False


class SimPoller:
    def __init__(self, cam):
        self.cam = cam
        self.history = []

    def latest(self, max_age=1.0):
        with self.cam.lock:
            p = CamPos(*self.cam.pos, time.monotonic())
        self.history.append(p)
        self.history = self.history[-100:]
        return p

    def settled(self, seconds=0.5, tol=0.05):
        now = time.monotonic()
        recent = [h for h in self.history if now - h.t <= seconds]
        if len(recent) < 3 or recent[0].t > now - seconds * 0.7:
            return False
        return all(max(getattr(h, a) for h in recent) - min(getattr(h, a) for h in recent) < tol for a in ("pan", "tilt", "hfov"))


HOME = {"pan": 4.99, "tilt": -4.06, "hfov": 44.8}


def run_homing(mode, stop_burst_first=False):
    cam = SimCamera(mode)
    with cam.lock:
        cam.presets[10] = [HOME["pan"], HOME["tilt"], HOME["hfov"]]
    poller = SimPoller(cam)
    if stop_burst_first:
        cam.stop()  # what the tracker was doing just before deciding to go home
    h = Homing(cam, poller, 10, HOME)
    t0 = time.monotonic()
    while not h.step():
        poller.latest()
        time.sleep(1 / 30)  # one step per video frame
        if time.monotonic() - t0 > 30:
            break
    p = poller.latest()
    cam.close()
    err = (abs(p.pan - HOME["pan"]), abs(p.tilt - HOME["tilt"]), abs(p.hfov - HOME["hfov"]))
    return h.result, time.monotonic() - t0, err, cam


def old_behaviour(mode):
    """What the first version did: stop burst immediately followed by the recall."""
    cam = SimCamera(mode)
    with cam.lock:
        cam.presets[10] = [HOME["pan"], HOME["tilt"], HOME["hfov"]]
    cam.stop()
    cam.recall_preset(10)
    time.sleep(2.0)
    with cam.lock:
        p = list(cam.pos)
    cam.close()
    return p


def main():
    failures = 0
    print("old code, camera that drops a recall sent right after stops:")
    p = old_behaviour("busy_drop")
    print(f"   ended at pan {p[0]:+.2f} tilt {p[1]:+.2f} view {p[2]:.1f} (home is {HOME})  <- the bug")

    for mode, want in (("obey", "preset"), ("busy_drop", "preset"), ("ignore", "driven there")):
        result, took, err, cam = run_homing(mode, stop_burst_first=True)
        ok = result == want and err[0] < 0.4 and err[1] < 0.4 and err[2] < 1.0
        failures += not ok
        print(
            f"{'PASS' if ok else 'FAIL'}  camera {mode:9s} -> {result!r:16s} in {took:4.1f}s  "
            f"error pan {err[0]:.2f}° tilt {err[1]:.2f}° view {err[2]:.2f}°  "
            f"(recalls accepted {cam.recalls_accepted}, dropped {cam.recalls_dropped})"
        )

    # geometry round trip: pixel -> angle -> pixel must come back to the same pixel
    from ptztracker.stage import angle_to_pixel, pixel_to_angle

    worst = 0.0
    for hfov in (63.4, 44.8, 18.3, 3.4):
        pos = CamPos(7.0, -5.0, hfov, 0)
        for x, y in ((0, 0), (640, 360), (320, 180), (100, 300), (600, 20)):
            px = angle_to_pixel(*pixel_to_angle(x, y, pos, 640, 360), pos, 640, 360)
            worst = max(worst, abs(px[0] - x), abs(px[1] - y))
    ok = worst < 1e-6
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  geometry round trip, worst error {worst:.2e} px")

    # boundary drawn at one zoom must land on the same scene points after pan + zoom
    wide = CamPos(4.99, -4.06, 44.8, 0)
    b = StageBounds.from_rect(20, 30, 615, 240, wide, 640, 360)
    tele = CamPos(14.0, -2.0, 30.0, 0)
    centre_angle = pixel_to_angle(300, 135, wide, 640, 360)
    moved = angle_to_pixel(*centre_angle, tele, 640, 360)
    back = pixel_to_angle(*moved, tele, 640, 360)
    ok = abs(back[0] - centre_angle[0]) < 1e-6 and b.contains(*back)
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  stage point stays on stage after pan+zoom ({moved[0]:.0f},{moved[1]:.0f} px)")

    print("all passed" if failures == 0 else f"{failures} FAILED")
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
