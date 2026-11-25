"""Presets and key handling in the window: 20 slots, bank switching, Shift+digit store, names, the stage slot.

    QT_QPA_PLATFORM=offscreen .venv/bin/python -m tests.test_presets
"""
import os
import sys
import tempfile

os.environ["HOME"] = os.environ["APPDATA"] = os.environ["LOCALAPPDATA"] = tempfile.mkdtemp()  # never touch the real config
os.environ["PTZ_NO_UPDATE_CHECK"] = "1"

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from ptztracker import app as appmod
from ptztracker import keys
from ptztracker import ptz as ptzmod

ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(("PASS" if cond else "FAIL") + f"  {name}" + (f" ({detail})" if detail and not cond else ""))


class Cam(ptzmod.PTZBase):
    def __init__(self):
        self.log = []

    def recall_preset(self, n, speed=1.0):
        self.log.append(("recall", n))

    def store_preset(self, n):
        self.log.append(("store", n))

    def home(self):
        self.log.append(("home",))

    def one_push_af(self):
        self.log.append(("af",))

    def focus(self, s):
        self.log.append(("focus", s))

    def zoom(self, s):
        self.log.append(("zoom", s))


def main():
    app = QApplication.instance() or QApplication([])
    w = appmod.MainWindow()
    w.isActiveWindow = lambda: True
    w.cam = Cam()

    def key(k, press=True, shift=False):
        mods = Qt.ShiftModifier if shift else Qt.NoModifier
        e = QKeyEvent(QEvent.KeyPress if press else QEvent.KeyRelease, int(k), mods)
        return w.eventFilter(None, e)

    check("20 preset buttons in a 5 x 4 grid", len(w.preset_btns) == 20)
    key(Qt.Key_3)
    check("key 3 recalls preset 3", w.cam.log[-1] == ("recall", 3), w.cam.log)
    key(Qt.Key_0)
    check("key 0 recalls preset 10", w.cam.log[-1] == ("recall", 10))
    key(Qt.Key_At, shift=True)
    check("Shift+2 (arrives as @) stores preset 2", w.cam.log[-1] == ("store", 2), w.cam.log)

    w.binds.assign("bank", 0, keys.key_code("N"))  # bank has no key by default
    w._binds_changed()
    key(Qt.Key_N)
    check("the bank key switches to bank 2 and the hint says 11-20", w.bank == 1 and "11–20" in w.bank_hint.text(), w.bank_hint.text())
    key(Qt.Key_3)
    check("bank 2: key 3 recalls preset 13", w.cam.log[-1] == ("recall", 13))
    key(Qt.Key_0)
    check("bank 2: key 0 recalls preset 20", w.cam.log[-1] == ("recall", 20))
    key(Qt.Key_Exclam, shift=True)
    check("bank 2: Shift+1 stores preset 11", w.cam.log[-1] == ("store", 11))
    check("bank 2 slots are lit, bank 1 dimmed", "color" not in w.preset_btns[12].styleSheet() and "color" in w.preset_btns[2].styleSheet())
    key(Qt.Key_N)
    check("pressing it again goes back to bank 1", w.bank == 0 and "1–10" in w.bank_hint.text())

    # names
    w._set_preset_name(3, "Pulpit")
    check("name shows under the number", w.preset_btns[2].text() == "3\nPulpit" and w.cfg["preset_names"] == {"3": "Pulpit"}, w.preset_btns[2].text())
    w._set_preset_name(3, "  ")
    check("empty name clears it", w.preset_btns[2].text() == "3" and "3" not in w.cfg["preset_names"])

    # stage slot
    w.stage.home_preset = 10
    w._refresh_presets()
    check("the stage slot is labelled Stage", w.preset_btns[9].text() == "10\nStage")
    asked = []
    QMessageBox.question = staticmethod(lambda *a, **k: (asked.append(a[1]), QMessageBox.No)[1])
    n = len(w.cam.log)
    key(Qt.Key_ParenRight, shift=True)
    check("storing into the stage slot asks first, and No keeps it", asked and len(w.cam.log) == n, asked)
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
    key(Qt.Key_ParenRight, shift=True)
    check("Yes stores it", w.cam.log[-1] == ("store", 10))

    # other keys come from the binds
    key(Qt.Key_H)
    check("H still acts", ("home",) in w.cam.log)
    key(Qt.Key_E)
    key(Qt.Key_E, press=False)
    check("E zooms in and releasing stops", w.cam.log[-2][0] == "zoom" and w.cam.log[-2][1] > 0 and w.cam.log[-1] == ("zoom", 0), w.cam.log[-2:])
    key(Qt.Key_BracketLeft)
    check("[ focuses near", w.cam.log[-1] == ("focus", -0.5))
    w.binds.assign("focus_once", 0, keys.key_code("J"))
    w._binds_changed()
    n = len(w.cam.log)
    key(Qt.Key_J)
    check("a key bound to focus once works", w.cam.log[n:] == [("af",)], w.cam.log[n:])
    from PySide6.QtWidgets import QPushButton

    once = [b for b in w.rail.findChildren(QPushButton) if b.text().startswith("Once")]
    check("button labels follow the binds", once and once[0].text().endswith("J"), [b.text() for b in once])
    step, zoom = w.pt_step, w.zoom_speed
    key(Qt.Key_V)
    check("V lowers the pan/tilt step, not zoom", w.pt_step == step - 1 and w.zoom_speed == zoom)
    key(Qt.Key_B)
    key(Qt.Key_B)
    check("B raises it", w.pt_step == step + 1)
    key(Qt.Key_F)
    check("F lowers zoom speed by 10 %, not pan/tilt", abs(w.zoom_speed - (zoom - 0.1)) < 1e-9 and w.pt_step == step + 1)
    key(Qt.Key_G)
    key(Qt.Key_G)
    check("G raises it", abs(w.zoom_speed - (zoom + 0.1)) < 1e-9 and w.view._toast[0] == f"Zoom speed {round((zoom + 0.1) * 100)} %", w.view._toast)
    check("unbound key is left alone", key(Qt.Key_J, press=False) is False and key(Qt.Key_K) is False)
    w.cam.log.clear()
    w.eventFilter(None, QKeyEvent(QEvent.KeyPress, int(Qt.Key_Comma), Qt.ControlModifier))
    check("Ctrl+, opens settings", w.settings_dialog.isVisible())

    # Canon API: names and recall speed come from the camera, unless set here
    class CanonCam(Cam):
        caps = frozenset({"preset_speed"})

        def recall_preset(self, n, speed=None):
            self.log.append(("recall", n, speed))

        def store_preset(self, n, name=None, speed=None):
            self.log.append(("store", n, speed))

    w.cam = CanonCam()
    w.canon_api = object()  # any client: _canon_update only checks that one is connected
    w.cfg["preset_names"].pop("4", None)
    w._canon_update({"p.4.name.utf8": "Lectern", "p.5.name.utf8": "Choir"})
    check("the camera's preset names show on the grid", "Lectern" in w.preset_btns[3].text() and w.preset_name(5) == "Choir", w.preset_btns[3].text())
    w._set_preset_name(5, "Band")
    check("a name given here wins over the camera's", w.preset_name(5) == "Band" and "Band" in w.preset_btns[4].text())
    w._set_preset_speed(4, 30)
    w.preset(4)
    w.preset(5)
    check("recall sends a preset's own speed, else the camera's saved one", w.cam.log[-2:] == [("recall", 4, 30), ("recall", 5, None)], w.cam.log[-2:])
    w.preset(4, store=True)
    check("storing keeps the preset's speed", w.cam.log[-1] == ("store", 4, 30), w.cam.log[-1])
    check("the speed shows in the tooltip", "Recall speed 30" in w.preset_btns[3].toolTip())
    w._set_preset_speed(4, None)
    w.preset(4)
    check("clearing it goes back to the camera's saved speed", w.cam.log[-1] == ("recall", 4, None) and "4" not in w.cfg["preset_speeds"])
    w.cam = Cam()
    w._set_preset_speed(6, 50)
    w.preset(6)
    check("NDI / VISCA recall ignores Canon preset speeds", w.cam.log[-1] == ("recall", 6), w.cam.log[-1])
    w.worker.stop()
    print("all passed" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
