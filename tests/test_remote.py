"""Remote control: message checks, the hold watchdog, the server (pairing, PIN lockout, WebSocket) and the window glue.

    QT_QPA_PLATFORM=offscreen .venv/bin/python -m tests.test_remote
"""
import asyncio
import os
import socket
import sys
import tempfile
import time

os.environ["HOME"] = os.environ["APPDATA"] = os.environ["LOCALAPPDATA"] = tempfile.mkdtemp()  # never touch the real config
os.environ["PTZ_NO_UPDATE_CHECK"] = "1"

import numpy as np
from PySide6.QtWidgets import QApplication

from ptztracker import app as appmod
from ptztracker import ptz as ptzmod
from ptztracker import remote as rm

ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(("PASS" if cond else "FAIL") + f"  {name}" + (f" ({detail})" if detail and not cond else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Cam(ptzmod.PTZBase):
    def __init__(self):
        self.log = []

    def pan_tilt(self, pan, tilt):
        self.log.append(("pt", round(pan, 3), round(tilt, 3)))

    def zoom(self, s):
        self.log.append(("zoom", s))

    def focus(self, s):
        self.log.append(("focus", s))

    def stop(self):
        self.log.append(("stop",))

    def recall_preset(self, n, speed=1.0):
        self.log.append(("recall", n))

    def store_preset(self, n):
        self.log.append(("store", n))

    def one_push_af(self):
        self.log.append(("af",))


def pump(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


def test_messages():
    c = rm.clean_command
    check("joy is clamped to -1..1", c({"t": "joy", "x": 5, "y": -9}) == {"t": "joy", "x": 1.0, "y": -1.0})
    check("joy with junk becomes a stop", c({"t": "joy", "x": "a", "y": float("nan")}) == {"t": "joy", "x": 0.0, "y": 0.0})
    check("zoom is reduced to -1/0/1", c({"t": "zoom", "d": 0.4}) == {"t": "zoom", "d": 1} and c({"t": "zoom", "d": -3})["d"] == -1)
    check("unknown types and actions are ignored", c({"t": "rm -rf"}) is None and c({"t": "act", "a": "quit"}) is None and c([1]) is None)
    check("preset outside 1-20 is ignored", c({"t": "act", "a": "preset", "n": 21}) is None and c({"t": "act", "a": "preset", "n": True}) is None)
    check("preset store flag kept", c({"t": "act", "a": "preset", "n": 3, "store": 1}) == {"t": "act", "a": "preset", "n": 3, "store": True})
    check("pick is clamped to the frame", c({"t": "act", "a": "pick", "x": 1.5, "y": -1}) == {"t": "act", "a": "pick", "x": 1.0, "y": 0.0})


def test_watchdog():
    w = rm.HoldWatchdog(0.3)
    w.touch("a", "joy", True, now=0.0)
    w.touch("b", "zoom", True, now=0.0)
    check("nothing expires inside the timeout", w.expired(now=0.25) == [])
    w.touch("a", "joy", True, now=0.25)
    check("a quiet hold expires, a refreshed one doesn't", w.expired(now=0.4) == [("b", "zoom")])
    w.touch("a", "joy", False)
    check("letting go forgets it", not w)
    w.touch("a", "joy", True, now=1)
    w.touch("a", "focus", True, now=1)
    check("a disconnect drops every hold of that device", sorted(w.drop("a")) == [("a", "focus"), ("a", "joy")] and not w)


def test_server(app):
    import aiohttp

    bridge = rm.RemoteBridge()
    got, left, paired = [], [], []
    bridge.command.connect(lambda cid, m: got.append(m))
    bridge.client_left.connect(left.append)
    bridge.paired.connect(lambda t, n: paired.append(n))
    port = free_port()
    srv = rm.RemoteServer(bridge, port, "4817")
    srv.start()
    check("server starts", srv.running, srv.error)
    base = f"http://127.0.0.1:{port}"

    async def run():
        async with aiohttp.ClientSession() as s:
            async with s.get(base + "/") as r:
                check("page is served", r.status == 200 and "PTZ Remote" in await r.text())
            async with s.get(base + "/ws") as r:
                check("socket refused without a token", r.status == 401)
            async with s.post(base + "/api/pair", json={"pin": "0000"}) as r:
                check("wrong PIN refused", r.status == 403)
            async with s.post(base + "/api/pair", json={"pin": "4817", "name": "Test iPad"}) as r:
                token = (await r.json())["token"]
                check("right PIN gives a token", r.status == 200 and len(token) == 32)
            async with s.get(base + "/api/check", params={"token": token}) as r:
                check("token checks out", r.status == 200)
            async with s.ws_connect(base + f"/ws?token={token}") as ws:
                await ws.send_json({"t": "ping", "ts": 5})
                msg = await ws.receive_json(timeout=2)
                check("ping answered", msg == {"t": "pong", "ts": 5}, msg)
                await ws.send_json({"t": "joy", "x": 0.5, "y": 0})
                await ws.send_json({"t": "bogus"})
                await asyncio.sleep(0.2)
            await asyncio.sleep(0.2)
            for _ in range(rm.PIN_TRIES):
                async with s.post(base + "/api/pair", json={"pin": "9999"}):
                    pass
            async with s.post(base + "/api/pair", json={"pin": "4817"}) as r:
                check("too many wrong PINs locks pairing, even for the right one", r.status == 429)
            return token

    loop = asyncio.new_event_loop()
    token = loop.run_until_complete(run())
    pump(app, 0.3)
    check("device commands reach the Qt side, junk doesn't", got == [{"t": "joy", "x": 0.5, "y": 0.0}], got)
    check("leaving is reported", len(left) == 1)
    check("pairing is reported with the device name", paired == ["Test iPad"])

    srv.set_tokens({})

    async def unpaired():
        async with aiohttp.ClientSession() as s:
            async with s.get(base + "/api/check", params={"token": token}) as r:
                return r.status

    check("Forget all invalidates old tokens", loop.run_until_complete(unpaired()) == 401)
    loop.close()
    srv.stop()
    check("server stops", not srv.running)


def test_window(app):
    w = appmod.MainWindow()
    w.cam = cam = Cam()
    r = w.remote

    r.on_command("c1", {"t": "joy", "x": 1.0, "y": 0.0})
    pump(app, 0.15)
    check("joystick moves the camera and pauses tracking output", any(e[0] == "pt" and e[1] > 0 for e in cam.log) and w.worker.manual_active, cam.log[-3:])
    check("held by the remote", "remote:c1" in w.held)
    pump(app, 0.5)  # no repeats: the watchdog lets go
    check("a joystick that goes quiet is let go", "remote:c1" not in w.held)
    pump(app, 0.6)
    check("...and the camera eases to a stop", cam.log[-1] == ("pt", 0.0, 0.0), cam.log[-2:])

    cam.log.clear()
    r.on_command("c1", {"t": "zoom", "d": 1})
    check("zoom in", cam.log and cam.log[-1][0] == "zoom" and cam.log[-1][1] > 0)
    r.on_command("c2", {"t": "zoom", "d": 0})
    check("another device letting go doesn't stop this zoom", w.zoom_held == 1)
    r.on_left("c1")
    check("disconnecting stops its zoom", w.zoom_held == 0 and cam.log[-1] == ("zoom", 0))

    r.on_command("c1", {"t": "focus", "d": -1})
    pump(app, 0.5)
    check("focus that goes quiet is stopped", cam.log[-1] == ("focus", 0), cam.log[-2:])

    cam.log.clear()
    r.on_command("c1", {"t": "act", "a": "preset", "n": 4, "store": False})
    check("preset recall", cam.log == [("recall", 4)], cam.log)
    r.on_command("c1", {"t": "act", "a": "preset", "n": 4, "store": True})
    check("store refused while remotes may not store", ("store", 4) not in cam.log)
    r.cfg["allow_store"] = True
    r.on_command("c1", {"t": "act", "a": "preset", "n": w.stage.home_preset, "store": True})
    check("stage shot slot never stored from a remote (no dialog on the PC)", ("store", w.stage.home_preset) not in cam.log)
    r.on_command("c1", {"t": "act", "a": "preset", "n": 5, "store": True})
    check("store allowed when turned on", ("store", 5) in cam.log)

    r.on_command("c1", {"t": "act", "a": "track"})
    check("track toggles tracking on", w.track_btn.isChecked())
    step = w.pt_step
    r.on_command("c1", {"t": "act", "a": "speed", "n": 1})
    r.on_command("c1", {"t": "act", "a": "speed", "n": 1})
    r.on_command("c1", {"t": "act", "a": "speed", "n": -1})
    check("Faster / Slower step pan/tilt by one", w.pt_step == step + 1, w.pt_step)
    r.on_command("c1", {"t": "act", "a": "auto_pick", "on": False})
    check("auto-pick off", not w.auto_pick.isChecked() and not w.worker.auto_pick)
    r.on_command("c1", {"t": "joy", "x": 0.3, "y": 0.3})
    r.on_command("c1", {"t": "act", "a": "stop"})
    check("Stop ends tracking and every hold", not w.track_btn.isChecked() and not w.held and cam.log[-1] == ("stop",))

    clicks = []
    w.worker.click = lambda x, y: clicks.append((x, y))
    r.frame({"frame": np.zeros((720, 1280, 3), np.uint8), "dets": np.zeros((0, 5), np.float32)})  # no server: nothing sent
    r._frame_size = (1280, 720)
    r.on_command("c1", {"t": "act", "a": "pick", "x": 0.5, "y": 0.25})
    check("tap on the preview picks at frame pixels", clicks == [(640.0, 180.0)], clicks)

    ov = r.overlay({"dets": np.array([[128, 72, 256, 144, 0.9]], np.float32), "box": np.array([128, 72, 256, 144]), "lost": False}, 1280, 720)
    check("overlay boxes are in 0..1 frame units", ov["dets"] == [[0.1, 0.1, 0.2, 0.2]] and ov["box"] == [0.1, 0.1, 0.2, 0.2], ov)

    s = r.state()
    check("state carries tracking, speed and the stage slot", s["speed"] == f"Pan/tilt {step + 1} · Zoom 50 %" and s["stage_preset"] == w.stage.home_preset and s["tracking"] is False)

    # enabling from the tab starts the server and keeps the PIN
    r.cfg["port"] = free_port()
    r.enable_check.setChecked(True)
    check("turning it on starts the server", r.server is not None and r.server.running, r.server and r.server.error)
    check("the tab shows the address and PIN", r.url_label.text().startswith("http://") and r.pin_label.text() == r.cfg["pin"])
    old = r.cfg["pin"]
    while r.cfg["pin"] == old:
        r.new_pin()
    check("New PIN reaches the server", r.server.pin == r.cfg["pin"])
    r.enable_check.setChecked(False)
    check("turning it off stops it", r.server is None)
    w.worker.stop()


def main():
    app = QApplication.instance() or QApplication([])
    test_messages()
    test_watchdog()
    test_server(app)
    test_window(app)
    print("all passed" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
