"""Canon web API client against a fake camera (stdlib HTTP server, no hardware): session, parsing, newest-write-wins,
RCCA speed maths, preset recall and store queries, picture writes, and close.

    .venv/bin/python -m tests.test_canon_http
"""
import http.server
import sys
import threading
import time
import urllib.parse

from ptztracker import canon_http as ch
from ptztracker import motion

ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(("PASS" if cond else "FAIL") + f"  {name}" + (f" ({detail})" if detail and not cond else ""))


INFO = {
    "c.1.pan": "1250", "c.1.tilt": "-300", "c.1.zoom": "6000",
    "c.1.pan.speed.min": "12", "c.1.tilt.speed.min": "12", "c.1.zoom.speed.max": "15", "c.1.zoom.speed.min": "0",
    "p.1.name.utf8": "Pulpit", "p.2.name.utf8": "Choir", "p.ptzspeed.min": "1", "p.ptzspeed.max": "100",
    "p.ptzspeed.saved.list": "on,off", "c.1.me.brightness": "0", "c.1.me.brightness.min": "-12", "c.1.me.brightness.max": "12",
}  # fmt: skip


class Camera(http.server.BaseHTTPRequestHandler):
    requests = []  # (path, params)
    slow_control = 0.0

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path.split("/-wvhttp-01-/", 1)[-1]
        q = dict(urllib.parse.parse_qsl(u.query))
        Camera.requests.append((path, q))
        if path == "open":
            body = "s=abc123\ns.authority.control:=enabled\n"
        elif path == "info":
            items = q["item"].split(",") if "item" in q else INFO
            body = "".join(f"{k}:={INFO[k]}\n" for k in items if k in INFO)
        elif path in ("control", "preset/set", "close"):
            time.sleep(Camera.slow_control)
            body = "OK\n"
        else:
            self.send_error(404)
            return
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def wait(cond, secs=3.0):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def sent(path="control"):
    return [q for p, q in Camera.requests if p == path]


def main():
    check("parse takes :=, == and =", ch.parse("a:=1\nb==2\nc=3\nnoise\n") == {"a": "1", "b": "2", "c": "3"})
    check("pan/tilt step 1 sends the camera minimum", ch.pt_wire(motion.PT_STEP1, 12) == 12)
    check("pan/tilt steps 2..16 match RCCA floor(5000*(L-1)/15)",
          all(ch.pt_wire((L - 1) / 15, 12) == max(12, (5000 * (L - 1)) // 15) for L in range(2, 17)))  # fmt: skip
    check("pan/tilt never goes past 5000", ch.pt_wire(1.7, 12) == 5000)
    check("zoom 50 % of max 15 is 7 (floor of 7.5)", ch.zoom_wire(0.5, 15) == 7)
    check("zoom 100 % is the max", ch.zoom_wire(1.0, 15) == 15)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Camera)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host = f"127.0.0.1:{srv.server_address[1]}"
    states = []
    c = ch.CanonClient(host, interval=0.02, on_state=states.append)
    cam = ch.CanonAPI(c)

    check("reads the position", wait(lambda: c.latest() is not None) and c.latest().pan == 12.5 and c.latest().hfov == 60.0)
    check("reads the whole info list and reports it", wait(lambda: states) and states[0]["p.1.name.utf8"] == "Pulpit")
    check("no session is opened just to read", not sent("open"))

    cam.pan_tilt(-(8 - 1) / 15, 0)
    check("a command opens the session first", wait(lambda: sent()) and sent("open"))
    q = sent()[-1]
    check("pan left at RCCA step 8 with zoom-linked speed",
          q.get("pan") == "left" and q.get("pan.speed.dir") == str(5000 * 7 // 15) and q.get("pan.speed.mode.dir") == "auto2"
          and q.get("tilt") == "stop" and q.get("s") == "abc123", q)  # fmt: skip

    Camera.requests.clear()
    Camera.slow_control = 0.2  # the camera is busy, so commands pile up
    for v in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6):
        cam.zoom(v)
        time.sleep(0.03)
    Camera.slow_control = 0.0
    check("a burst of zoom changes ends on the newest", wait(lambda: sent() and sent()[-1].get("zoom.speed") == "9"), sent())
    check("and skips the stale ones in between", len(sent()) < 6, len(sent()))

    Camera.requests.clear()
    cam.recall_preset(3, speed=40)
    check("recall at a set speed", wait(lambda: sent()) and sent()[-1] == {"p": "3", "p.ptzspeed": "40", "s": "abc123"}, sent())
    Camera.requests.clear()
    cam.recall_preset(2)
    check("recall at the preset's saved speed", wait(lambda: sent()) and sent()[-1].get("p.ptzspeed.saved") == "on", sent())
    Camera.requests.clear()
    cam.store_preset(2, speed=25)
    q = wait(lambda: sent("preset/set")) and sent("preset/set")[-1]
    check("store keeps the camera's name and sends the speed", q and q["name"] == "Choir" and q["ptz.speed"] == "25", q)
    Camera.requests.clear()
    cam.set("me.brightness", 4)
    check("picture write by control name", wait(lambda: sent()) and sent()[-1].get("me.brightness") == "4", sent())

    Camera.requests.clear()
    cam.close()
    check("close stops the camera and ends the session",
          wait(lambda: sent("close")) and any(q.get("pan") == "stop" for q in sent()) and sent("close")[0].get("s") == "abc123")  # fmt: skip
    n = len(Camera.requests)
    time.sleep(0.2)
    check("and the poll thread stops", len(Camera.requests) <= n + 1, len(Camera.requests) - n)
    srv.shutdown()

    print("all passed" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
