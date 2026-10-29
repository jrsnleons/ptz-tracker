"""Camera control backends.

Internal convention for every backend:
    pan  in [-1, 1]  (+ = right)
    tilt in [-1, 1]  (+ = up)
    zoom in [-1, 1]  (+ = tele / zoom in)
    focus in [-1, 1] (+ = far)
"""
import collections
import socket
import threading
import time


class PTZBase:
    name = "None"

    def pan_tilt(self, pan, tilt):
        pass

    def zoom(self, speed):
        pass

    def focus(self, speed):
        pass

    def autofocus(self, on=True):
        pass

    def one_push_af(self):
        pass

    def home(self):
        pass

    def recall_preset(self, n, speed=1.0):
        pass

    def store_preset(self, n):
        pass

    def stop(self):
        self.pan_tilt(0, 0)
        self.zoom(0)

    def close(self):
        pass

    def status(self):
        return "no camera control"


class VISCAoverIP(PTZBase):
    """Sony-style VISCA over IP (UDP 52381). The Canon CR-N300 supports this once
    "VISCA over IP" is enabled in the camera's web settings."""

    name = "VISCA over IP"
    PAN_MAX = 0x18
    TILT_MAX = 0x14

    ERRORS = {
        0x01: "message length error", 0x02: "syntax error", 0x03: "command buffer full",
        0x04: "command cancelled", 0x05: "no socket", 0x41: "command not executable",
    }  # fmt: skip

    def __init__(self, host, port=52381, raw=False):
        self.addr = (host, int(port))
        self.raw = raw  # some cameras want bare VISCA bytes without the IP header
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # the CR-N300 sends its replies to port 52381 rather than back to our source port,
        # so listen there when we can; otherwise commands still work, we just can't see errors
        try:
            self.sock.bind(("", int(port)))
            self.listening = True
        except OSError:
            self.listening = False
        self.sock.setblocking(False)
        self.replies = collections.deque(maxlen=50)  # (time, seq, "ack"|"done"|"error", text)
        self.last_error = None
        self.seq = 0
        self.lock = threading.Lock()
        self._last_pt = None
        self._last_zoom = None
        self._last_focus = None
        self.sent = 0
        self._reset_sequence()

    def _reset_sequence(self):
        if self.raw:
            return
        with self.lock:
            # control command: RESET sequence number
            pkt = bytes([0x02, 0x00, 0x00, 0x01]) + (0).to_bytes(4, "big") + b"\x01"
            self._send_raw(pkt)
            self.seq = 0

    def _send_raw(self, pkt):
        try:
            self.sock.sendto(pkt, self.addr)
            self.sent += 1
        except OSError:
            pass

    def send(self, visca: bytes):
        with self.lock:
            if self.raw:
                self._send_raw(visca)
                return
            self.seq = (self.seq + 1) & 0xFFFFFFFF
            hdr = bytes([0x01, 0x00]) + len(visca).to_bytes(2, "big") + self.seq.to_bytes(4, "big")
            self._send_raw(hdr + visca)
            self._drain()

    def _drain(self):
        """Reads ACK / completion / error replies so the buffer never fills and errors are visible."""
        try:
            while True:
                data, addr = self.sock.recvfrom(1024)
                if addr[0] != self.addr[0]:
                    continue
                self._parse_reply(data)
        except (BlockingIOError, OSError):
            pass

    def _parse_reply(self, data):
        seq = int.from_bytes(data[4:8], "big") if len(data) >= 8 and data[0] == 0x01 else None
        body = data[8:] if seq is not None else data
        if len(body) < 3 or (body[0] & 0xF0) != 0x90:
            return
        kind = body[1] & 0xF0
        if kind == 0x40:
            self.replies.append((time.monotonic(), seq, "ack", ""))
        elif kind == 0x50:
            self.replies.append((time.monotonic(), seq, "done", body.hex(" ")))
        elif kind == 0x60:
            text = self.ERRORS.get(body[2], f"error 0x{body[2]:02x}")
            self.replies.append((time.monotonic(), seq, "error", text))
            self.last_error = (time.monotonic(), text)

    def poll_replies(self):
        with self.lock:
            self._drain()

    def pan_tilt(self, pan, tilt):
        ps = round(abs(pan) * self.PAN_MAX)
        ts = round(abs(tilt) * self.TILT_MAX)
        pd = 0x03 if ps == 0 else (0x02 if pan > 0 else 0x01)
        td = 0x03 if ts == 0 else (0x01 if tilt > 0 else 0x02)
        key = (max(ps, 1), max(ts, 1), pd, td)
        if key == self._last_pt:
            return
        self._last_pt = key
        cmd = bytes([0x81, 0x01, 0x06, 0x01, key[0], key[1], pd, td, 0xFF])
        self.send(cmd)
        if pd == 0x03 and td == 0x03:
            self.send(cmd)  # UDP can drop packets and a missed stop is the worst case

    def zoom(self, speed):
        p = min(7, round(abs(speed) * 7))
        if speed == 0:
            cmd = 0x00
        else:
            cmd = (0x20 if speed > 0 else 0x30) | p
        if cmd == self._last_zoom:
            return
        self._last_zoom = cmd
        self.send(bytes([0x81, 0x01, 0x04, 0x07, cmd, 0xFF]))

    def focus(self, speed):
        p = min(7, round(abs(speed) * 7))
        cmd = 0x00 if speed == 0 else ((0x20 if speed > 0 else 0x30) | p)
        if cmd == self._last_focus:
            return
        self._last_focus = cmd
        if cmd != 0:
            self.autofocus(False)
        self.send(bytes([0x81, 0x01, 0x04, 0x08, cmd, 0xFF]))

    def autofocus(self, on=True):
        self.send(bytes([0x81, 0x01, 0x04, 0x38, 0x02 if on else 0x03, 0xFF]))

    def one_push_af(self):
        self.send(bytes([0x81, 0x01, 0x04, 0x18, 0x01, 0xFF]))

    def home(self):
        self._last_pt = None
        self.send(bytes([0x81, 0x01, 0x06, 0x04, 0xFF]))

    def recall_preset(self, n, speed=1.0):
        # the preset move replaces any manual motion, so forget what we last sent
        self._last_pt = None
        self._last_zoom = None
        self.send(bytes([0x81, 0x01, 0x04, 0x3F, 0x02, n & 0x7F, 0xFF]))
        return self.seq

    def store_preset(self, n):
        self.send(bytes([0x81, 0x01, 0x04, 0x3F, 0x01, n & 0x7F, 0xFF]))

    def stop(self):
        self._last_pt = None
        self._last_zoom = None
        self.pan_tilt(0, 0)
        self.zoom(0)

    def close(self):
        try:
            self.stop()
        finally:
            self.sock.close()

    def status(self):
        self.poll_replies()
        text = f"VISCA {self.addr[0]}:{self.addr[1]} ({self.sent} packets sent"
        text += ", replies on)" if self.listening else ", replies off)"
        if self.last_error and time.monotonic() - self.last_error[0] < 30:
            text += f" — camera said: {self.last_error[1]}"
        return text


class NDIPTZ(PTZBase):
    """PTZ through the NDI connection (what the Windows tracker used)."""

    name = "NDI PTZ"

    def __init__(self, ndi_source):
        self.src = ndi_source
        self._last = None
        self._last_zoom = None

    @property
    def ptz(self):
        r = self.src.receiver
        return r.ptz if r is not None else None

    def _ok(self):
        r = self.src.receiver
        return r is not None and r.is_connected() and r.is_ptz_supported()

    def pan_tilt(self, pan, tilt):
        key = (round(pan, 3), round(tilt, 3))
        if key == self._last or not self._ok():
            return
        self._last = key
        # NDI: pan +1 = left, tilt +1 = up
        self.ptz.pan_and_tilt(-pan, tilt)

    def zoom(self, speed):
        key = round(speed, 3)
        if key == self._last_zoom or not self._ok():
            return
        self._last_zoom = key
        self.ptz.zoom(speed)

    def focus(self, speed):
        if self._ok():
            self.ptz.focus(speed)

    def autofocus(self, on=True):
        if self._ok() and on:
            self.ptz.autofocus()

    def one_push_af(self):
        self.autofocus(True)

    def home(self):
        if self._ok():
            self._last = None
            self.ptz.set_pan_and_tilt_values(0.0, 0.0)

    def recall_preset(self, n, speed=1.0):
        if self._ok():
            self._last = None
            self._last_zoom = None
            self.ptz.recall_preset(n, speed)

    def store_preset(self, n):
        if self._ok():
            self.ptz.store_preset(n)

    def stop(self):
        self._last = None
        self._last_zoom = None
        self.pan_tilt(0, 0)
        self.zoom(0)

    def status(self):
        r = self.src.receiver
        if r is None or not r.is_connected():
            return "NDI PTZ: not connected"
        return "NDI PTZ: ready" if r.is_ptz_supported() else "NDI PTZ: source reports no PTZ support"
