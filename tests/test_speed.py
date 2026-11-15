"""Speed levels and smoother manual motion: config migration, ramp, diagonals, zoom scaling, and the window's tick.

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
    # migration of the old single slider
    check("default levels", [d["pan"] for d in motion.DEFAULT_LEVELS] == [0.03, 0.08, 0.18, 0.35, 0.7] and [d["zoom"] for d in motion.DEFAULT_LEVELS] == [0.15, 0.25, 0.4, 0.6, 1.0])
    for old, want in ((0.05, 1), (0.5, 4), (1.0, 5), (0.1, 2), (0.2, 3), (0.3, 4), (0.6, 5)):
        got = motion.migrate_speed({"manual_speed": old})["speed_level"]
        check(f"manual_speed {old} -> level {want}", got == want, got)
    c = motion.migrate_speed({"manual_speed": 0.5})
    check("old key is dropped and levels are filled in", "manual_speed" not in c and len(c["speed_levels"]) == 5)
    check("a saved speed_level wins over manual_speed", motion.migrate_speed({"manual_speed": 0.05, "speed_level": 5})["speed_level"] == 5)
    check("fresh config starts at Medium", motion.migrate_speed({})["speed_level"] == 3)
    check("out-of-range level is clamped", motion.migrate_speed({"speed_level": 9})["speed_level"] == 5)
    c = motion.migrate_speed({"speed_level": 4})
    check("configs from before the sliders start at their level's speeds", c["pan_speed"] == 0.35 and c["zoom_speed"] == 0.6, c)
    c = motion.migrate_speed({"speed_level": 4, "pan_speed": 0.22, "zoom_speed": 5})
    check("saved slider speeds are kept, and clamped", c["pan_speed"] == 0.22 and c["zoom_speed"] == 1.0, c)
    check("broken slider speeds fall back to the level", motion.migrate_speed({"pan_speed": "x", "zoom_speed": True})["pan_speed"] == 0.18)
    L = motion.DEFAULT_LEVELS
    check("Slower / Faster jump to the next stop from between two", motion.step_level(0.25, L, -1) == 3 and motion.step_level(0.25, L, 1) == 4)
    check("... and from on a stop", motion.step_level(0.18, L, -1) == 2 and motion.step_level(0.18, L, 1) == 4)
    check("... and stop at the ends", motion.step_level(0.01, L, -1) == 1 and motion.step_level(1.0, L, 1) == 5)
    check("broken speed_levels are repaired", motion.clean_levels([{"pan": "x"}, 5])[0] == motion.DEFAULT_LEVELS[0] and len(motion.clean_levels(None)) == 5)

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

    check("window starts at Medium", w.speed_level == 3 and w.pan_slider.slider.value() == 18 and w.zoom_slider.slider.value() == 40)
    w.set_speed_level(2)
    w.step_speed(-1)
    w.step_speed(-1)
    check("slower never goes under 1 (no wraparound)", w.speed_level == 1 and w.pan_slider.slider.value() == 3)
    for _ in range(9):
        w.step_speed(1)
    check("faster stops at 5", w.speed_level == 5 and w.pan_speed == 0.7 and w.zoom_speed == 1.0)
    check("toast text appears on the video", w.view._toast[0] == "Speed 5 · Max", w.view._toast)

    w.set_speed_level(3)
    w.manual_press("k", 1, 0)
    first = w.cam.calls[0]
    pump(0.6)
    check("a held key eases in to the level's speed", first[0] < 0.18 and abs(w.cam.pt[0] - 0.18) < 1e-6, (first, w.cam.pt))
    check("tracking is held off while moving", w.worker.manual_active)
    w.set_speed_level(5)
    pump(0.6)
    check("changing level while held takes effect at once", abs(w.cam.pt[0] - 0.7) < 1e-6, w.cam.pt)
    w.manual_release("k")
    pump(0.4)
    check("release eases back to a stop", w.cam.pt == (0.0, 0.0) and not w.ramp.active and not w.ramp_timer.isActive())
    check("tracking is free again afterwards", not w.worker.manual_active)

    w.set_speed_level(3)
    w.manual_press("a", 1, 0)
    w.manual_press("b", 0, 1)
    pump(0.6)
    mag = (w.cam.pt[0] ** 2 + w.cam.pt[1] ** 2) ** 0.5
    check("diagonal keys move at the straight-line speed", abs(mag - 0.18) < 1e-3 and abs(w.cam.pt[0] - w.cam.pt[1]) < 1e-6, w.cam.pt)

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
    w.set_speed_level(3)
    w.manual_press("k", 1, 0)
    pump(0.5)
    wide = w.cam.pt[0]
    w.poller.hfov = 15.0
    pump(0.5)
    tele = w.cam.pt[0]
    check("full tele moves slower than wide", abs(wide - 0.18) < 1e-6 and abs(tele - 0.18 * 0.25) < 1e-6, (wide, tele))
    w.cfg["zoom_scaled_speed"] = False
    pump(0.5)
    check("the toggle turns zoom scaling off", abs(w.cam.pt[0] - 0.18) < 1e-6, w.cam.pt)
    w.manual_release("k")
    pump(0.4)
    w.poller = None
    w.zoom_press(1)
    check("zoom uses the level's zoom speed", abs(w.cam.z - 0.4) < 1e-9, w.cam.z)
    w.zoom_slider.slider.setValue(77)  # dragging the zoom slider
    check("the zoom slider changes a zoom in progress, not pan/tilt", abs(w.cam.z - 0.77) < 1e-9 and w.pan_speed == 0.18 and w.cfg["zoom_speed"] == 0.77, (w.cam.z, w.pan_speed))
    w.zoom_release()
    w.pan_slider.slider.setValue(25)  # dragging the pan/tilt slider
    w.manual_press("k", 1, 0)
    pump(0.5)
    check("the pan/tilt slider sets the move speed, not the zoom", abs(w.cam.pt[0] - 0.25) < 1e-6 and w.zoom_speed == 0.77 and w.cfg["pan_speed"] == 0.25, w.cam.pt)
    w.manual_release("k")
    pump(0.4)
    w.step_speed(1)
    check("Faster from between two stops goes to the next one up", w.speed_level == 4 and w.pan_speed == 0.35 and w.zoom_speed == 0.6)

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
    check("Save writes the speeds and keys without closing Settings", saved["pan_speed"] == 0.35 and saved["keys"]["home"][0] == "J" and w.save_btn.text().startswith("Saved"))
    w.worker.stop()


if __name__ == "__main__":
    main()
