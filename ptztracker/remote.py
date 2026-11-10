"""Remote control from a phone or tablet on the same network.

An aiohttp server runs in its own thread with its own asyncio loop. It serves one page (web/remote.html), pairs devices
with a PIN, and carries a WebSocket per device. Messages from devices reach the Qt thread through RemoteBridge.command;
the window sends state and preview frames back with broadcast_state() / push_frame(), which are safe to call from Qt.

Safety: the page re-sends every held control (joystick, zoom, focus) about every 100 ms. HoldWatchdog, run by the window
on a Qt timer, releases a hold that has gone quiet for HOLD_TIMEOUT, and a disconnect releases everything that device held.
"""
import asyncio
import json
import logging
import os
import secrets
import socket
import sys
import threading
import time

from PySide6.QtCore import QObject, Signal

log = logging.getLogger("ptztracker.remote")

DEFAULT_PORT = 8765
HOLD_TIMEOUT = 0.3  # seconds without a repeat before a held control is let go
PREVIEW_FPS = {"off": 0, "low": 5, "normal": 10}
PREVIEW_WIDTH = 640
PIN_TRIES = 5  # wrong PINs in a row before pairing locks for PIN_LOCKOUT seconds
PIN_LOCKOUT = 30.0
HOLD_KINDS = ("joy", "zoom", "focus")
ACTIONS = {"track", "release", "stop", "home", "stage_shot", "focus_once", "focus_auto", "speed", "auto_pick", "pick", "preset"}


def new_pin():
    return f"{secrets.randbelow(10000):04d}"


def lan_ip():
    """This machine's address on the local network (no packet is sent), or 127.0.0.1 when offline."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def web_dir():
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    frozen = os.path.join(base, "ptztracker", "web")
    return frozen if os.path.isdir(frozen) else os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def clamp(v, lo=-1.0, hi=1.0):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if v != v else max(lo, min(hi, v))  # NaN -> 0


def clean_command(msg):
    """Validate one message from a device. Returns a normalised dict, or None to ignore it."""
    if not isinstance(msg, dict):
        return None
    t = msg.get("t")
    if t == "joy":
        return {"t": "joy", "x": clamp(msg.get("x")), "y": clamp(msg.get("y"))}
    if t in ("zoom", "focus"):
        d = clamp(msg.get("d"))
        return {"t": t, "d": (d > 0) - (d < 0)}
    if t == "act" and msg.get("a") in ACTIONS:
        a = msg["a"]
        out = {"t": "act", "a": a}
        if a == "speed":
            n = msg.get("n")
            if not isinstance(n, int) or isinstance(n, bool):
                return None
            out["n"] = n
        elif a == "auto_pick":
            out["on"] = bool(msg.get("on"))
        elif a == "pick":
            out["x"], out["y"] = clamp(msg.get("x"), 0.0, 1.0), clamp(msg.get("y"), 0.0, 1.0)
        elif a == "preset":
            n = msg.get("n")
            if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 20:
                return None
            out["n"], out["store"] = n, bool(msg.get("store"))
        return out
    return None


class HoldWatchdog:
    """Remembers when each device last repeated each held control, and reports the ones that went quiet."""

    def __init__(self, timeout=HOLD_TIMEOUT):
        self.timeout = timeout
        self.seen = {}  # (client, kind) -> monotonic time of the last repeat

    def touch(self, client, kind, active, now=None):
        if active:
            self.seen[(client, kind)] = time.monotonic() if now is None else now
        else:
            self.seen.pop((client, kind), None)

    def drop(self, client):
        """Every hold this device had (it disconnected)."""
        gone = [k for k in self.seen if k[0] == client]
        for k in gone:
            del self.seen[k]
        return gone

    def expired(self, now=None):
        now = time.monotonic() if now is None else now
        gone = [k for k, t in self.seen.items() if now - t > self.timeout]
        for k in gone:
            del self.seen[k]
        return gone

    def __bool__(self):
        return bool(self.seen)


class RemoteBridge(QObject):
    """Lives in the Qt thread. Signals emitted from the server thread arrive queued on the Qt thread."""

    command = Signal(str, dict)  # client id, cleaned message
    client_left = Signal(str)
    clients_changed = Signal()
    paired = Signal(str, str)  # token, device name
    client_hello = Signal(str)  # a device connected: send it the state


class Client:
    def __init__(self, cid, ws, name):
        self.id = cid
        self.ws = ws
        self.name = name
        self.since = time.time()
        self.last_cmd = 0.0
        self.frame = None  # latest (overlay, jpeg) waiting to go out
        self.wake = asyncio.Event()


class RemoteServer:
    def __init__(self, bridge: RemoteBridge, port=DEFAULT_PORT, pin=None, tokens=None):
        self.bridge = bridge
        self.port = port
        self.pin = pin or new_pin()
        self._tokens = dict(tokens or {})  # token -> device name
        self._lock = threading.Lock()
        self.clients = {}  # id -> Client (touched only on the server loop)
        self.preview = "normal"
        self.error = ""
        self._fails = 0
        self._locked_until = 0.0
        self._loop = None
        self._thread = None
        self._runner = None
        self._ready = threading.Event()
        self._last_frame_at = 0.0
        self._state = {}

    # ---------------- called from the Qt thread
    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive() and not self.error

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self.error = ""
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="remote-server", daemon=True)
        self._thread.start()
        self._ready.wait(5)

    def stop(self):
        loop, thread = self._loop, self._thread
        if loop and thread and thread.is_alive():
            asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
            thread.join(3)
        self._thread = None
        self._loop = None

    def set_tokens(self, tokens):
        with self._lock:
            self._tokens = dict(tokens)
        self._call(self._kick_unknown)

    def set_pin(self, pin):
        with self._lock:
            self.pin = pin
            self._fails = 0
            self._locked_until = 0.0

    def kick(self, cid):
        self._call(lambda: self._close(cid))

    def client_list(self):
        """[(id, name, controlling recently)] — read from the Qt thread; a stale view is fine."""
        now = time.monotonic()
        return [(c.id, c.name, now - c.last_cmd < 3.0) for c in list(self.clients.values())]

    def broadcast_state(self, state):
        self._state = state
        self._call(lambda: self._send_all({"t": "state", **state}))

    def send(self, cid, msg):
        self._call(lambda: self._send_one(cid, msg))

    def wants_frame(self, now=None):
        fps = PREVIEW_FPS.get(self.preview, 0)
        if not fps or not self.clients or not self._loop:
            return False
        now = time.monotonic() if now is None else now
        return now - self._last_frame_at >= 1.0 / fps

    def push_frame(self, frame, overlay):
        """frame: BGR numpy image. Encoded on the server thread so the GUI never waits on JPEG."""
        self._last_frame_at = time.monotonic()
        self._call(lambda: self._encode_and_queue(frame, overlay))

    # ---------------- server thread
    def _call(self, fn):
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(fn)
            except RuntimeError:
                pass  # loop closing

    def _run(self):
        from aiohttp import web

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/manifest.json", self._manifest)
        app.router.add_get("/icon.png", self._icon)
        app.router.add_post("/api/pair", self._pair)
        app.router.add_get("/api/check", self._check)
        app.router.add_get("/ws", self._ws)
        self._runner = web.AppRunner(app, access_log=None)
        try:
            loop.run_until_complete(self._runner.setup())
            site = web.TCPSite(self._runner, host="0.0.0.0", port=self.port, reuse_address=True)
            loop.run_until_complete(site.start())
        except OSError as e:
            self.error = f"Port {self.port} is in use or blocked ({e.strerror or e})"
            log.warning("remote server could not start: %s", e)
            self._ready.set()
            loop.run_until_complete(self._runner.cleanup())
            loop.close()
            self._loop = None
            return
        log.info("remote server on port %s", self.port)
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            loop.close()

    async def _shutdown(self):
        for c in list(self.clients.values()):
            await c.ws.close()
        if self._runner:
            await self._runner.cleanup()
        asyncio.get_running_loop().stop()

    async def _index(self, request):
        from aiohttp import web

        path = os.path.join(web_dir(), "remote.html")
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})

    async def _manifest(self, request):
        from aiohttp import web

        return web.json_response(
            {"name": "PTZ Remote", "short_name": "PTZ Remote", "start_url": "/", "display": "standalone",
             "background_color": "#121314", "theme_color": "#121314",
             "icons": [{"src": "/icon.png", "sizes": "512x512", "type": "image/png"}]}
        )  # fmt: skip

    async def _icon(self, request):
        from aiohttp import web

        base = getattr(sys, "_MEIPASS", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
        path = os.path.join(base, "assets", "icon.png")
        if not os.path.exists(path):
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    def _check_pin(self, pin, now):
        """'ok' | 'wrong' | 'locked'. Several wrong PINs in a row lock pairing for a while."""
        with self._lock:
            if now < self._locked_until:
                return "locked"
            if isinstance(pin, str) and secrets.compare_digest(pin.strip(), self.pin):
                self._fails = 0
                return "ok"
            self._fails += 1
            if self._fails >= PIN_TRIES:
                self._fails = 0
                self._locked_until = now + PIN_LOCKOUT
            return "wrong"

    async def _pair(self, request):
        from aiohttp import web

        try:
            body = await request.json()
        except Exception:
            body = {}
        result = self._check_pin(body.get("pin"), time.monotonic())
        if result == "locked":
            return web.json_response({"error": "Too many wrong PINs. Try again in 30 seconds."}, status=429)
        if result == "wrong":
            return web.json_response({"error": "Wrong PIN"}, status=403)
        name = str(body.get("name") or "Device")[:40]
        token = secrets.token_hex(16)
        with self._lock:
            self._tokens[token] = name
        self.bridge.paired.emit(token, name)
        return web.json_response({"token": token})

    async def _check(self, request):
        """Lets the page tell 'not paired any more' apart from 'PC unreachable' before opening the socket."""
        from aiohttp import web

        ok = self._token_name(request.query.get("token")) is not None
        return web.json_response({"ok": ok}, status=200 if ok else 401)

    def _token_name(self, token):
        with self._lock:
            return self._tokens.get(token) if isinstance(token, str) else None

    async def _ws(self, request):
        from aiohttp import WSMsgType, web

        name = self._token_name(request.query.get("token"))
        if name is None:
            return web.json_response({"error": "Not paired"}, status=401)
        ws = web.WebSocketResponse(heartbeat=5.0, max_msg_size=64 * 1024)
        await ws.prepare(request)
        cid = secrets.token_hex(4)
        client = Client(cid, ws, name)
        client.token = request.query.get("token")
        self.clients[cid] = client
        sender = asyncio.ensure_future(self._frame_sender(client))
        self.bridge.clients_changed.emit()
        if self._state:
            await self._safe_send(client, {"t": "state", **self._state})
        self.bridge.client_hello.emit(cid)
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                if isinstance(data, dict) and data.get("t") == "ping":
                    await self._safe_send(client, {"t": "pong", "ts": data.get("ts")})
                    continue
                cmd = clean_command(data)
                if cmd is not None:
                    client.last_cmd = time.monotonic()
                    self.bridge.command.emit(cid, cmd)
        finally:
            sender.cancel()
            self.clients.pop(cid, None)
            self.bridge.client_left.emit(cid)
            self.bridge.clients_changed.emit()
        return ws

    async def _safe_send(self, client, msg):
        try:
            await client.ws.send_str(json.dumps(msg))
        except Exception:
            pass

    def _send_all(self, msg):
        for c in list(self.clients.values()):
            asyncio.ensure_future(self._safe_send(c, msg))

    def _send_one(self, cid, msg):
        c = self.clients.get(cid)
        if c:
            asyncio.ensure_future(self._safe_send(c, msg))

    def _close(self, cid):
        c = self.clients.get(cid)
        if c:
            asyncio.ensure_future(c.ws.close(code=4001, message=b"Disconnected from the PC"))

    def _kick_unknown(self):
        for c in list(self.clients.values()):
            if self._token_name(getattr(c, "token", None)) is None:
                asyncio.ensure_future(c.ws.close(code=4003, message=b"Unpaired"))

    def _encode_and_queue(self, frame, overlay):
        if not self.clients:
            return
        try:
            import cv2

            h, w = frame.shape[:2]
            if w > PREVIEW_WIDTH:
                frame = cv2.resize(frame, (PREVIEW_WIDTH, int(h * PREVIEW_WIDTH / w)), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 62])
        except Exception:
            log.debug("preview encode failed", exc_info=True)
            return
        if not ok:
            return
        item = (json.dumps({"t": "ov", **overlay}), buf.tobytes())
        for c in self.clients.values():
            c.frame = item  # a slow device just gets the newest frame next, never a backlog
            c.wake.set()

    async def _frame_sender(self, client):
        try:
            while True:
                await client.wake.wait()
                client.wake.clear()
                item, client.frame = client.frame, None
                if item is None:
                    continue
                try:
                    await client.ws.send_str(item[0])
                    await client.ws.send_bytes(item[1])
                except Exception:
                    return
        except asyncio.CancelledError:
            pass
