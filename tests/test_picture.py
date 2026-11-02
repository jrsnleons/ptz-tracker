"""Picture commands reach the camera: VISCA bytes through a fake socket, NDI calls on a fake receiver, throttling.

    QT_QPA_PLATFORM=offscreen .venv/bin/python -m tests.test_picture
"""
import os
import sys
import tempfile
import threading

os.environ["HOME"] = os.environ["APPDATA"] = os.environ["LOCALAPPDATA"] = tempfile.mkdtemp()  # never touch the real config
os.environ["PTZ_NO_UPDATE_CHECK"] = "1"

from ptztracker import picture
from ptztracker import ptz as ptzmod

ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(("PASS" if cond else "FAIL") + f"  {name}" + (f" ({detail})" if detail and not cond else ""))


class FakeSock:
    def __init__(self):
        self.sent = []

    def sendto(self, pkt, addr):
        self.sent.append(bytes(pkt))

    def recvfrom(self, n):
        raise BlockingIOError

    def close(self):
        pass


def visca():
    v = ptzmod.VISCAoverIP("127.0.0.1", 0, raw=True)  # raw: the packets are the bare VISCA bytes
    v.sock.close()
    v.sock = FakeSock()
    return v, v.sock


def hx(*b):
    return bytes(b)


class FakePTZ:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *a: self.calls.append((name, *a))


class FakeReceiver:
    def __init__(self, connected=True):
        self.ptz = FakePTZ()
        self.connected = connected

    def is_connected(self):
        return self.connected

    def is_ptz_supported(self):
        return True


class FakeSource:
    def __init__(self, receiver):
        self.receiver = receiver
        self.lock = threading.RLock()


def main():
    v, sock = visca()
    v.exposure_mode("auto")
    v.exposure_mode("manual")
    check("AE mode auto / manual", sock.sent == [hx(0x81, 1, 4, 0x39, 0x00, 0xFF), hx(0x81, 1, 4, 0x39, 0x03, 0xFF)], sock.sent)
    sock.sent.clear()
    v.exposure_level(1.0)
    check("brightness: exposure compensation on, then +7 = 0x0E", sock.sent == [hx(0x81, 1, 4, 0x3E, 0x02, 0xFF), hx(0x81, 1, 4, 0x4E, 0, 0, 0, 0x0E, 0xFF)], sock.sent)
    sock.sent.clear()
    v.exposure_level(0.0)
    check("brightness -7 = 0", sock.sent[1] == hx(0x81, 1, 4, 0x4E, 0, 0, 0, 0, 0xFF))
    sock.sent.clear()
    v.exposure_fine(1.0, 0.0, 0.0)
    check("iris / gain / shutter direct (4B, 4C, 4A) in 0p 0q nibbles", sock.sent == [hx(0x81, 1, 4, 0x4B, 0, 0, 1, 1, 0xFF), hx(0x81, 1, 4, 0x4C, 0, 0, 0, 1, 0xFF), hx(0x81, 1, 4, 0x4A, 0, 0, 0, 0, 0xFF)], sock.sent)
    sock.sent.clear()
    for mode in ("auto", "indoor", "outdoor", "manual"):
        v.white_balance_mode(mode)
    check("WB modes 00 / 01 / 02 / 05", [p[4] for p in sock.sent] == [0x00, 0x01, 0x02, 0x05] and all(p[:4] == hx(0x81, 1, 4, 0x35) for p in sock.sent), sock.sent)
    sock.sent.clear()
    v.white_balance_mode("once")
    check("WB one-push sets mode 03 then triggers it", sock.sent == [hx(0x81, 1, 4, 0x35, 0x03, 0xFF), hx(0x81, 1, 4, 0x10, 0x05, 0xFF)], sock.sent)
    sock.sent.clear()
    v.white_balance_rgb(1.0, 0.5)
    check("manual R/B gain switches to manual first, then 43 / 44", sock.sent == [hx(0x81, 1, 4, 0x35, 0x05, 0xFF), hx(0x81, 1, 4, 0x43, 0, 0, 0x0F, 0x0F, 0xFF), hx(0x81, 1, 4, 0x44, 0, 0, 0x08, 0x00, 0xFF)], sock.sent)
    sock.sent.clear()
    v.white_balance_rgb(0.0, 0.0)
    check("already manual: just the gains", len(sock.sent) == 2)
    sock.sent.clear()
    v.aperture(1.0)
    check("aperture (sharpness) direct 42", sock.sent == [hx(0x81, 1, 4, 0x42, 0, 0, 0, 0x0F, 0xFF)], sock.sent)
    check("VISCA advertises exposure, wb and aperture", v.caps == {"exposure", "wb", "aperture"})
    sock.sent.clear()
    v.exposure_level(5)  # out of range is clamped
    check("out-of-range levels are clamped", sock.sent[1][7] == 0x0E)

    # IP-framed packets carry the same bytes after the 8 byte header
    v2, s2 = visca()
    v2.raw = False
    v2.exposure_mode("auto")
    check("with the VISCA-over-IP header", s2.sent[0][8:] == hx(0x81, 1, 4, 0x39, 0, 0xFF) and s2.sent[0][:2] == hx(0x01, 0x00), s2.sent)

    # NDI
    rx = FakeReceiver()
    n = ptzmod.NDIPTZ(FakeSource(rx))
    check("NDI advertises exposure and wb, not aperture", n.caps == {"exposure", "wb"})
    n.exposure_mode("auto")
    n.exposure_level(0.25)
    n.exposure_fine(0.1, 0.2, 0.3)
    n.exposure_mode("manual")
    n.white_balance_mode("auto")
    n.white_balance_mode("indoor")
    n.white_balance_mode("outdoor")
    n.white_balance_mode("once")
    n.white_balance_rgb(0.7, 0.3)
    n.white_balance_mode("manual")
    n.aperture(0.5)  # NDI can't: must not raise or send
    want = [("exposure_auto",), ("set_exposure_coarse", 0.25), ("set_exposure_fine", 0.1, 0.2, 0.3), ("set_exposure_fine", 0.1, 0.2, 0.3),
            ("white_balance_auto",), ("white_balance_indoor",), ("white_balance_outdoor",), ("white_balance_oneshot",),
            ("set_white_balance", 0.7, 0.3), ("set_white_balance", 0.7, 0.3)]  # fmt: skip
    check("NDI calls map to the receiver's PTZ object", rx.ptz.calls == want, rx.ptz.calls)
    rx.connected = False
    rx.ptz.calls.clear()
    n.exposure_level(0.5)
    n.white_balance_mode("auto")
    check("nothing is sent while NDI is not connected", rx.ptz.calls == [])

    # Guarded: failures are logged, and the picture commands are guarded names
    class Broken(ptzmod.PTZBase):
        def exposure_level(self, level):
            raise RuntimeError("boom")

    g = ptzmod.Guarded(Broken())
    g.exposure_level(0.5)
    check("a failing picture command does not raise through Guarded", True)
    check("Guarded passes caps through", ptzmod.Guarded(v).caps == v.caps)
    check("picture commands are in COMMANDS", {"exposure_mode", "exposure_level", "exposure_fine", "white_balance_mode", "white_balance_rgb", "aperture"} <= ptzmod.COMMANDS)
    base = ptzmod.PTZBase()
    base.exposure_mode("auto"), base.exposure_level(1), base.exposure_fine(0, 0, 0), base.white_balance_mode("auto"), base.white_balance_rgb(0, 0), base.aperture(0)
    check("no camera: picture commands are no-ops", base.caps == frozenset())

    # saved values
    c = picture.clean({"exposure": "manual", "iris": 3, "gain": "x", "wb": "bogus", "unknown": 1})
    check("saved values are cleaned", c["exposure"] == "manual" and c["iris"] == 1.0 and c["gain"] == picture.DEFAULTS["gain"] and c["wb"] == "auto" and "unknown" not in c, c)
    check("reset to auto only touches the modes", picture.reset_to_auto({**c})["exposure"] == "auto" and picture.reset_to_auto(c)["iris"] == 1.0)
    v3, s3 = visca()
    picture.send_all(v3, picture.clean({"exposure": "manual", "wb": "manual", "sharpness": 0.0}))
    check("a look sends modes, then their values", [p[3] for p in s3.sent] == [0x39, 0x4B, 0x4C, 0x4A, 0x35, 0x43, 0x44, 0x42], [hex(p[3]) for p in s3.sent])

    # Canon HTTP: parsing and finding items (no network)
    text = "c.1.pan:=100\nc.1.picture.contrast:=3\nc.1.picture.contrast.max:=10\nc.1.sharpness:=2\nc.1.color.saturation:=0\nx:=y\n"
    d = picture.CanonPicture.parse(text)
    items = picture.CanonPicture.find_items(d)
    check("info.cgi lines parse", d["c.1.pan"] == "100" and len(d) == 6)
    check("contrast / sharpness / saturation items are found, helper keys skipped", items == {"contrast": "c.1.picture.contrast", "sharpness": "c.1.sharpness", "saturation": "c.1.color.saturation"}, items)
    check("no matching items -> nothing to show", picture.CanonPicture.find_items({"c.1.pan": "1"}) == {})

    window_checks()
    print("all passed" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)


def window_checks():
    import time

    from PySide6.QtWidgets import QApplication

    from ptztracker import app as appmod

    app = QApplication.instance() or QApplication([])
    w = appmod.MainWindow()
    check("picture tab disabled with no camera", not w.pic_rows["brightness"].isEnabled() and not w.pic_modes["exposure"]["auto"].isEnabled() and "Connect" in w.pic_note.text())
    check("contrast / saturation hidden when the camera has none", w.profile_box.isHidden())

    v, sock = visca()
    w.cam = ptzmod.Guarded(v)
    w.pic_key = "cam-1"
    w._picture_refresh()
    check("controls enable for a VISCA camera", w.pic_rows["brightness"].isEnabled() and not w.pic_rows["iris"].isEnabled())
    check("sharpness shows for VISCA (aperture), contrast does not", not w.pic_rows["sharpness"].isHidden() and w.pic_rows["contrast"].isHidden())

    sock.sent.clear()
    for i in range(30):  # a drag
        w.pic_rows["brightness"].slider.setValue(30 + i)
    check("a drag sends nothing until the timer fires", sock.sent == [])
    t0 = time.monotonic()
    while not sock.sent and time.monotonic() - t0 < 1:
        app.processEvents()
        time.sleep(0.01)
    check("then one update with the newest value", len(sock.sent) == 2 and sock.sent[1][7] == round(0.59 * 14), sock.sent)
    check("value is remembered for this camera", abs(w.cfg["picture"]["cam-1"]["brightness"] - 0.59) < 1e-9)

    sock.sent.clear()
    w._pic_mode("exposure", "manual")
    check("Manual sends the mode and the iris/gain/shutter", [p[3] for p in sock.sent] == [0x39, 0x4B, 0x4C, 0x4A] and w.pic_rows["iris"].isEnabled() and not w.pic_rows["brightness"].isEnabled(), [hex(p[3]) for p in sock.sent])
    w.pic["wb"] = "outdoor"
    sock.sent.clear()
    w._pic_reset()
    check("Reset puts exposure and white balance back to auto", w.pic["exposure"] == w.pic["wb"] == "auto" and [p[3:5] for p in sock.sent] == [hx(0x39, 0x00), hx(0x35, 0x00)], sock.sent)

    w.cfg["looks"].clear()
    appmod.QInputDialog.getText = staticmethod(lambda *a, **k: ("Sunday morning", True))
    w.pic["iris"] = 0.9
    w._look_save()
    check("Save look stores the current values", w.cfg["looks"]["Sunday morning"]["iris"] == 0.9 and w.looks_combo.count() == 2)
    w.pic["iris"] = 0.1
    sock.sent.clear()
    w.looks_combo.setCurrentIndex(1)
    w._look_chosen(1)
    check("choosing a look sends it and moves the sliders", w.pic["iris"] == 0.9 and len(sock.sent) >= 4 and abs(w.pic_rows["iris"].slider.value() - 90) <= 1)
    w._look_delete()
    check("Delete removes the look", "Sunday morning" not in w.cfg["looks"])

    # NDI camera: no sharpness, explains why
    w.cam = ptzmod.Guarded(ptzmod.NDIPTZ(FakeSource(FakeReceiver())))
    w._picture_refresh()
    check("over NDI the profile sliders are hidden with an explanation", w.profile_box.isHidden() and "over NDI" in w.profile_note.text(), w.profile_note.text())
    w.worker.stop()


if __name__ == "__main__":
    main()
