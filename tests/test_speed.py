"""Speed steps and smoother manual motion: config migration, ramp, diagonals, zoom scaling, and the window's tick.

    QT_QPA_PLATFORM=offscreen .venv/bin/python -m tests.test_speed
"""
import os
import sys
import tempfile
import time

os.environ["HOME"] = os.environ["APPDATA"] = os.environ["LOCALAPPDATA"] = tempfile.mkdtemp()  # never touch the real config
os.environ["PTZ_NO_UPDATE_CHECK"] = "1"

from ptztracker import motion

ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(("PASS" if cond else "FAIL") + f"  {name}" + (f" ({detail})" if detail and not cond else ""))


def run_ramp(target, secs, dt=1 / 30, ramp=None):
    r = ramp or motion.Ramp()
    out = []
    for _ in range(round(secs / dt)):
        out.append(r.step(dt, *target))
    return r, out


def main():
    # speed steps (Canon's 1-16 for pan/tilt) and migration from older configs
    check("step L is (L - 1) / 15, step 1 a crawl", motion.pt_speed(16) == 1.0 and motion.pt_speed(8) == 7 / 15 and 0 < motion.pt_speed(1) < motion.pt_speed(2))
    check("steps past the ends are clamped", motion.pt_speed(40) == 1.0 and motion.clean_step(0) == 1 and motion.clean_step(99) == 16)
    c = motion.migrate_speed({})
    check("fresh config starts at step 8 and 50 % zoom", c["pt_step"] == 8 and c["zoom_speed"] == 0.5, c)
    c = motion.migrate_speed({"pan_speed": 0.35, "zoom_speed": 0.6, "speed_level": 4, "speed_levels": [], "manual_speed": 0.1})
    check("a 1.4 pan/tilt slider speed becomes the nearest step", c["pt_step"] == motion.nearest_step(0.35) == 6, c)
    check("its zoom speed is kept, the old stop keys are dropped", c["zoom_speed"] == 0.6 and not {"pan_speed", "speed_level", "speed_levels", "manual_speed"} & set(c), c)
    check("a 1.2 manual_speed also maps to a step", motion.migrate_speed({"manual_speed": 1.0})["pt_step"] == 16)
    check("a saved step wins and is clamped", motion.migrate_speed({"pt_step": 20, "pan_speed": 0.1})["pt_step"] == 16)
    check("broken values fall back", motion.migrate_speed({"pt_step": "x", "zoom_speed": True}) == {"pt_step": 8, "zoom_speed": 0.5})

    # ramp
    r, out = run_ramp((0.4, 0.0), 0.1)
    check("ramp does not jump to full speed", 0 < out[0][0] < 0.4 and out[-1][0] < 0.4, out[-1])
    check("ramp is monotonic going up", all(b[0] >= a[0] for a, b in zip(out, out[1:])))
    r, out = run_ramp((0.4, 0.0), 0.3)
    check("full speed after ~0.25 s", abs(out[-1][0] - 0.4) < 1e-9 and r.active)
    t_full = next(i for i, o in enumerate(out) if o[0] >= 0.4 - 1e-9) / 30
    check("ease in takes about 0.25 s", 0.2 <= t_full <= 0.3, t_full)
    _, down = run_ramp((0.0, 0.0), 0.3, ramp=r)
    t_stop = next(i for i, o in enumerate(down) if o[0] == 0) / 30
    check("ease out takes about 0.15 s", 0.1 <= t_stop <= 0.2, t_stop)
    check("ramp is idle again after stopping", not r.active)
    r = motion.Ramp()
    check("idle ramp is inactive", not r.active)
    r.step(1 / 30, 0.2, 0.0)
    check("ramp is active while moving", r.active)
    r, _ = run_ramp((0.4, -0.2), 0.4)
    _, flip = run_ramp((-0.4, 0.2), 0.05, ramp=r)
    check("reversing slows through zero rather than jumping", -0.4 < flip[0][0] < 0.4)
    r.reset()
    check("reset clears the ramp", not r.active and r.cur == [0.0, 0.0])

    # diagonals
    x, y = motion.normalise(1, 1)
    check("diagonal is no faster than straight", abs((x * x + y * y) ** 0.5 - 1) < 1e-9, (x, y))
    check("straight moves are untouched", motion.normalise(1, 0) == (1, 0) and motion.normalise(0, -1) == (0, -1))
    check("opposite keys cancel", motion.normalise(1 - 1, 0) == (0, 0))
    check("a gentle drag is not boosted", motion.normalise(0.3, 0.2) == (0.3, 0.2))
    check("sums over 1 are clipped", motion.normalise(2, 0) == (1, 0))

    # zoom scaling
    check("wide shot = full speed", motion.zoom_scale(60, 60) == 1.0)
    check("half the field of view = half speed", abs(motion.zoom_scale(30, 60) - 0.5) < 1e-9)
    check("scale never drops under 15 %", motion.zoom_scale(3, 60) == motion.MIN_ZOOM_SCALE)
    check("no readout = no scaling", motion.zoom_scale(None, 60) == 1.0 and motion.zoom_scale(30, 0) == 1.0)
    check("speed floor keeps crawl moving", motion.floor_speed(0.001) == motion.MIN_SPEED and motion.floor_speed(-0.001) == -motion.MIN_SPEED and motion.floor_speed(0) == 0)

    window_checks()
    print("all passed" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)


def window_checks():
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from ptztracker import app as appmod
    from ptztracker import ptz as ptzmod
    from ptztracker.stage import CamPos

    app = QApplication.instance() or QApplication([])

    class Cam(ptzmod.PTZBase):
        def __init__(self):
            self.pt, self.calls, self.z = (0.0, 0.0), [], 0.0

        def pan_tilt(self, pan, tilt):
            self.pt = (pan, tilt)
            self.calls.append((pan, tilt))

        def zoom(self, s):
            self.z = s

        def stop(self):
            self.pt = (0.0, 0.0)

    w = appmod.MainWindow()
    w.cam = Cam()

    def pump(secs):
        for _ in range(int(secs * 100)):
            app.processEvents()
            time.sleep(0.01)

    s8 = motion.pt_speed(8)
    check("window starts at step 8 and 50 %", w.pt_step == 8 and w.pan_slider.slider.value() == 8 and w.zoom_slider.slider.value() == 50, (w.pt_step, w.pan_slider.slider.value(), w.zoom_slider.slider.value(), w.cfg.get("zoom_speed")))
    w.set_speeds(step=2)
    w.step_speed(-1)
    w.step_speed(-1)
    check("slower never goes under 1 (no wraparound)", w.pt_step == 1 and w.pan_slider.slider.value() == 1)
    for _ in range(20):
        w.step_speed(1)
    check("faster stops at 16 and leaves zoom alone", w.pt_step == 16 and w.pan_speed == 1.0 and w.zoom_speed == 0.5)
    check("toast text appears on the video", w.view._toast[0] == "Pan / tilt speed 16", w.view._toast)

    w.set_speeds(step=8)
    w.manual_press("k", 1, 0)
    first = w.cam.calls[0]
    pump(0.6)
    check("a held key eases in to the step's speed", first[0] < s8 and abs(w.cam.pt[0] - s8) < 1e-6, (first, w.cam.pt))
    check("tracking is held off while moving", w.worker.manual_active)
    w.set_speeds(step=16)
    pump(0.6)
    check("changing speed while held takes effect at once", abs(w.cam.pt[0] - 1.0) < 1e-6, w.cam.pt)
    w.manual_release("k")
    pump(0.4)
    check("release eases back to a stop", w.cam.pt == (0.0, 0.0) and not w.ramp.active and not w.ramp_timer.isActive())
    check("tracking is free again afterwards", not w.worker.manual_active)

    w.set_speeds(step=8)
    w.manual_press("a", 1, 0)
    w.manual_press("b", 0, 1)
    pump(0.6)
    mag = (w.cam.pt[0] ** 2 + w.cam.pt[1] ** 2) ** 0.5
    check("diagonal keys move at the straight-line speed", abs(mag - s8) < 1e-3 and abs(w.cam.pt[0] - w.cam.pt[1]) < 1e-6, w.cam.pt)

    w.emergency_stop()
    check("emergency stop is immediate, no ramp", w.cam.pt == (0.0, 0.0) and not w.held and not w.ramp.active and not w.ramp_timer.isActive())

    # zoom-scaled speed
    class Poller:
        hfov = 60.0

        def latest(self, max_age=1.0):
            return CamPos(0, 0, self.hfov, time.monotonic())

        def status(self):
            return "fake"

        def close(self):
            pass

    w.poller = Poller()
    w.manual_press("k", 1, 0)
    pump(0.5)
    wide = w.cam.pt[0]
    w.poller.hfov = 15.0
    pump(0.5)
    tele = w.cam.pt[0]
    check("full tele moves slower than wide", abs(wide - s8) < 1e-6 and abs(tele - s8 * 0.25) < 1e-6, (wide, tele))
    w.cam.caps = frozenset({"zoom_linked"})
    pump(0.5)
    check("a camera that slows itself (Canon API) is not slowed twice", abs(w.cam.pt[0] - s8) < 1e-6, w.cam.pt)
    w.cam.caps = frozenset()
    w.cfg["zoom_scaled_speed"] = False
    pump(0.5)
    check("the toggle turns zoom scaling off", abs(w.cam.pt[0] - s8) < 1e-6, w.cam.pt)
    w.manual_release("k")
    pump(0.4)
    w.poller = None
    w.zoom_press(1)
    check("zoom uses the zoom speed", abs(w.cam.z - 0.5) < 1e-9, w.cam.z)
    w.zoom_slider.slider.setValue(77)  # dragging the zoom slider to 77 %
    check("the zoom slider changes a zoom in progress, not pan/tilt", abs(w.cam.z - 0.77) < 1e-9 and w.pt_step == 8 and w.cfg["zoom_speed"] == 0.77, (w.cam.z, w.pt_step))
    w.zoom_release()
    w.pan_slider.slider.setValue(12)  # dragging the pan/tilt slider to step 12
    w.manual_press("k", 1, 0)
    pump(0.5)
    check("the pan/tilt slider sets the move speed, not the zoom", abs(w.cam.pt[0] - motion.pt_speed(12)) < 1e-6 and w.zoom_speed == 0.77 and w.cfg["pt_step"] == 12, w.cam.pt)
    w.manual_release("k")
    pump(0.4)

    # key layouts
    w.binds.assign("home", 0, int(Qt.Key_J))
    w._binds_changed()
    w.cfg["key_layouts"]["Ana"] = w.binds.to_cfg()
    w._restore_keys()
    check("a saved layout is listed but not selected after going back to defaults", w.layouts_combo.findText("Ana") > 0 and w.layouts_combo.currentIndex() == 0)
    w._layout_chosen(w.layouts_combo.findText("Ana"))
    check("choosing a layout loads its keys and shows it selected", w.binds.keys["home"][0] == int(Qt.Key_J) and w.layouts_combo.currentText() == "Ana")
    w._layout_delete()
    check("deleting a layout keeps the keys in use", "Ana" not in w.cfg["key_layouts"] and w.binds.keys["home"][0] == int(Qt.Key_J))
    w._save_clicked()
    saved = appmod.load_config()
    check("Save writes the speeds and keys without closing Settings", saved["pt_step"] == 12 and saved["zoom_speed"] == 0.77 and saved["keys"]["home"][0] == "J" and w.save_btn.text().startswith("Saved"))
    w.worker.stop()


if __name__ == "__main__":
    main()
