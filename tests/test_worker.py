"""Worker behaviour with a fake video source and camera (no hardware needed).

    .venv/bin/python -m tests.test_worker
"""
import sys
import time

import numpy as np
from PySide6.QtCore import QCoreApplication

from ptztracker import ptz as ptzmod
from ptztracker.app import TrackerWorker
from ptztracker.sources import LatestFrame
from ptztracker.stage import StageSettings
from ptztracker.tracking import TrackSettings


class FakeDetector:
    provider = "fake"

    def __init__(self):
        self.x = 1400  # head right of centre, so tracking pans

    def detect(self, frame):
        return np.array([[self.x, 300, self.x + 80, 400, 0.9]], dtype=np.float32)


class FakeSource:
    name = "fake"

    def __init__(self):
        self.latest = LatestFrame()
        self.frame = np.zeros((1080, 1920, 3), np.uint8)
        self.live = True

    def feed(self):
        if self.live:
            self.latest.put(self.frame)

    def read(self, timeout=0.05):
        return self.latest.get(timeout)


class FakeCam(ptzmod.PTZBase):
    def __init__(self):
        self.pt = (0.0, 0.0)
        self.stops = 0
        self.recalls = 0

    def pan_tilt(self, pan, tilt):
        self.pt = (pan, tilt)

    def stop(self):
        self.stops += 1
        self.pt = (0.0, 0.0)

    def recall_preset(self, n, speed=1.0):
        self.recalls += 1


def pump(app, src, seconds, fps=30):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        src.feed()
        app.processEvents()
        time.sleep(1 / fps)


def main():
    app = QCoreApplication(sys.argv)
    w = TrackerWorker(FakeDetector(), TrackSettings(), StageSettings())
    src, cam = FakeSource(), FakeCam()
    events, frames = [], []
    w.stage_event.connect(events.append)
    w.frame_ready.connect(lambda d: frames.append(d))  # never clears gui_busy: a GUI that is stuck painting
    w.set_io(src, cam, None)
    w.tracking_enabled = True
    w.start()
    ok = True

    pump(app, src, 1.0)
    moving = cam.pt[0] > 0
    print(("PASS" if moving else "FAIL") + f"  tracking pans toward the head (pan {cam.pt[0]:+.2f})")
    ok &= moving

    stuck = len(frames) == 1
    print(("PASS" if stuck else "FAIL") + f"  stuck GUI gets 1 frame, not a backlog ({len(frames)} queued)")
    ok &= stuck

    src.live = False
    stops = cam.stops
    pump(app, src, 1.0)
    stopped = cam.stops > stops and cam.pt == (0.0, 0.0)
    print(("PASS" if stopped else "FAIL") + f"  video stall stops the camera ({events[-1] if events else 'no event'})")
    ok &= stopped

    # homing must finish even with no video arriving
    w.request_home()
    w._home_request = False
    w.go_home(cam, "test")
    pump(app, src, 1.5)
    homed = w.homing is None and cam.recalls == 1
    print(("PASS" if homed else "FAIL") + f"  homing completes with no video (recalls {cam.recalls}, state {w.stage_state})")
    ok &= homed

    w.stop()
    print("all passed" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
