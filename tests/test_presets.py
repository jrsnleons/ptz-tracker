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

    key(Qt.Key_B)
    check("B switches to bank 2 and the hint says 11-20", w.bank == 1 and "11–20" in w.bank_hint.text(), w.bank_hint.text())
    key(Qt.Key_3)
    check("bank 2: key 3 recalls preset 13", w.cam.log[-1] == ("recall", 13))
    key(Qt.Key_0)
    check("bank 2: key 0 recalls preset 20", w.cam.log[-1] == ("recall", 20))
    key(Qt.Key_Exclam, shift=True)
    check("bank 2: Shift+1 stores preset 11", w.cam.log[-1] == ("store", 11))
    check("bank 2 slots are lit, bank 1 dimmed", "color" not in w.preset_btns[12].styleSheet() and "color" in w.preset_btns[2].styleSheet())
    key(Qt.Key_B)
    check("B again goes back to bank 1", w.bank == 0 and "1–10" in w.bank_hint.text())

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
    key(Qt.Key_F)
    check("H and F still act", ("home",) in w.cam.log and ("af",) in w.cam.log)
    key(Qt.Key_E)
    key(Qt.Key_E, press=False)
    check("E zooms in and releasing stops", w.cam.log[-2][0] == "zoom" and w.cam.log[-2][1] > 0 and w.cam.log[-1] == ("zoom", 0), w.cam.log[-2:])
    key(Qt.Key_BracketLeft)
    check("[ focuses near", w.cam.log[-1] == ("focus", -0.5))
    w.binds.assign("focus_once", 0, keys.key_code("J"))
    w._binds_changed()
    n = len(w.cam.log)
    key(Qt.Key_F)
    key(Qt.Key_J)
    check("rebound key works, the old one does not", w.cam.log[n:] == [("af",)], w.cam.log[n:])
    from PySide6.QtWidgets import QPushButton

    once = [b for b in w.rail.findChildren(QPushButton) if b.text().startswith("Once")]
    check("button labels follow the binds", once and once[0].text().endswith("J"), [b.text() for b in once])
    key(Qt.Key_Z)
    check("Z lowers the speed level", w.speed_level == 2)
    key(Qt.Key_C)
    key(Qt.Key_C)
    check("C raises it", w.speed_level == 4)
    check("unbound key is left alone", key(Qt.Key_J, press=False) is False and key(Qt.Key_K) is False)
    w.cam.log.clear()
    w.eventFilter(None, QKeyEvent(QEvent.KeyPress, int(Qt.Key_Comma), Qt.ControlModifier))
    check("Ctrl+, opens settings", w.settings_dialog.isVisible())
    w.worker.stop()
    print("all passed" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
