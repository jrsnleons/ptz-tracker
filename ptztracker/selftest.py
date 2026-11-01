"""Smoke test of a built app: `PTZ Tracker.exe --selftest result.txt` (CI runs it after every Windows build).

Checks the parts that only break once packaged: the NDI library loads and discovery starts, the detector
runs on the bundled model, the icon ships, and frames from a source reach the video view.
"""
import threading
import time
import traceback

import numpy as np
from PySide6.QtWidgets import QApplication


def _fake_source():
    from .sources import LatestFrame

    class FakeSource:
        name = "self-test pattern"
        kind = "TEST"

        def __init__(self):
            self.latest = LatestFrame()
            self._run = True
            threading.Thread(target=self._loop, daemon=True).start()

        def _loop(self):
            frame = np.zeros((720, 1280, 3), np.uint8)
            frame[:, :, 1] = 90
            while self._run:
                self.latest.put(frame.copy())
                time.sleep(1 / 30)

        def read(self, timeout=0.05):
            return self.latest.get(timeout)

        def close(self):
            self._run = False

    return FakeSource()


def run(out_path):
    lines, ok = [], True

    def check(name, fn):
        nonlocal ok
        try:
            lines.append(f"PASS {name}: {fn()}")
        except Exception:
            ok = False
            lines.append(f"FAIL {name}:\n{traceback.format_exc()}")

    from . import app as appmod
    from . import ptz as ptzmod
    from .detector import HeadDetector, default_model_path
    from .sources import HAVE_NDI, NDIFinder

    def ndi():
        assert HAVE_NDI, "NDI library did not load"
        f = NDIFinder()
        names = f.names()
        f.close()
        return f"library loaded, discovery running ({len(names)} sources visible)"

    def detector():
        d = HeadDetector(default_model_path())
        dets = d.detect(np.random.randint(0, 255, (1080, 1920, 3), np.uint8))
        return f"{d.provider}, {len(dets)} detections on noise"

    def icon():
        import os

        p = appmod.resource_path("assets", "icon.png")
        assert os.path.exists(p), p
        return p

    def video_to_view():
        w = appmod.MainWindow()
        src = _fake_source()
        w.source = src
        w.worker.set_io(src, ptzmod.PTZBase())
        deadline = time.monotonic() + 15
        while w.view.image is None and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.01)
        img = w.view.image
        w.worker.set_io(None, None)
        src.close()
        w.worker.stop()
        assert img is not None, "no frame reached the video view in 15 s"
        return f"frame {img.width()}x{img.height()} painted, detector on {w.detector.provider}"

    check("NDI", ndi)
    check("detector", detector)
    check("icon", icon)
    check("video reaches the window", video_to_view)
    lines.append("OK" if ok else "FAILED")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 0 if ok else 1
