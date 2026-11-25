"""The PC side of remote control: the Settings → Remote tab, and turning device messages into the window's own actions.

Every remote action goes through the same MainWindow methods the keyboard and the rail use, so tracking pauses for a
manual move, presets stop tracking, and so on, exactly as on the PC.
"""
import logging
import socket
import time

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from . import remote as rm
from .stage import pixel_to_angle

log = logging.getLogger("ptztracker.remote")

DEFAULTS = {"enabled": False, "port": rm.DEFAULT_PORT, "pin": "", "tokens": {}, "preview": "normal", "allow_store": False}
PREVIEW_CHOICES = [("Off", "off"), ("Low · 5 fps", "low"), ("Normal · 10 fps", "normal")]


def qr_pixmap(text, size=200):
    """The QR code for text as a pixmap, or None when the qrcode package isn't available."""
    try:
        import qrcode
    except ImportError:
        return None
    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    m = qr.get_matrix()
    n = len(m)
    cell = max(1, size // n)
    pm = QPixmap(cell * n, cell * n)
    pm.fill(QColor("#EDEEF0"))
    p = QPainter(pm)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#121314"))
    for y, row in enumerate(m):
        for x, on in enumerate(row):
            if on:
                p.drawRect(x * cell, y * cell, cell, cell)
    p.end()
    return pm


class RemoteControl(QObject):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        cfg = win.cfg.setdefault("remote", {})
        for k, v in DEFAULTS.items():
            cfg.setdefault(k, v if not isinstance(v, dict) else dict(v))
        if not (isinstance(cfg["pin"], str) and len(cfg["pin"]) == 4 and cfg["pin"].isdigit()):
            cfg["pin"] = rm.new_pin()
        if cfg["preview"] not in rm.PREVIEW_FPS:
            cfg["preview"] = "normal"
        self.cfg = cfg
        self.bridge = rm.RemoteBridge()
        self.bridge.command.connect(self.on_command)
        self.bridge.client_left.connect(self.on_left)
        self.bridge.clients_changed.connect(self._clients_changed)
        self.bridge.paired.connect(self._paired)
        self.bridge.client_hello.connect(lambda _cid: self.push_state(force=True))
        self.server = None
        self.watch = rm.HoldWatchdog()
        self.watch_timer = QTimer(self, interval=50, timeout=self._check_holds)
        self.owner = {}  # "zoom"/"focus" -> client holding it, so one device letting go doesn't cancel another's
        self._sent = None
        self._frame_size = (0, 0)
        self.host = socket.gethostname().split(".")[0]

    # ---------------- server lifecycle
    def start(self):
        if self.server and self.server.running:
            return
        self.server = rm.RemoteServer(self.bridge, int(self.cfg["port"]), self.cfg["pin"], self.cfg["tokens"])
        self.server.preview = self.cfg["preview"]
        self.server.start()
        if self.server.error:
            self.win.statusBar().showMessage(f"Remote control couldn't start: {self.server.error}")
        else:
            self.push_state(force=True)
        self._refresh_tab()

    def stop(self):
        self.watch_timer.stop()
        self._release_everything()
        if self.server:
            self.server.stop()
        self.server = None
        self._refresh_tab()

    def shutdown(self):
        self.stop()

    def set_enabled(self, on):
        self.cfg["enabled"] = bool(on)
        self.start() if on else self.stop()
        self.win._save()

    # ---------------- state out
    def state(self):
        w = self.win
        names = {n: w.preset_name(n) for n in range(1, 21) if w.preset_name(n)}
        return {
            "tracking": bool(w.track_btn.isChecked()),
            "auto_pick": bool(w.auto_pick.isChecked()),
            "speed": f"Pan/tilt {w.pt_step} · Zoom {round(w.zoom_speed * 100)} %",
            "preset_names": names,
            "stage_preset": w.stage.home_preset,
            "camera": w.source.name if w.source is not None else "",
            "host": self.host,
            "clients": len(self.server.clients) if self.server else 0,
            "preview": rm.PREVIEW_FPS.get(self.cfg["preview"], 0) > 0,
            "allow_store": bool(self.cfg["allow_store"]),
        }

    def push_state(self, force=False):
        if not (self.server and self.server.running):
            return
        s = self.state()
        if force or s != self._sent:
            self._sent = s
            self.server.broadcast_state(s)

    def toast(self, cid, text):
        if self.server:
            self.server.send(cid, {"t": "toast", "text": text})

    def frame(self, data):
        """Called with every frame the window paints; sends a small copy when a device wants one."""
        srv = self.server
        frame = data.get("frame")
        if frame is None or not srv or not srv.wants_frame():
            return
        h, w = frame.shape[:2]
        self._frame_size = (w, h)
        try:
            srv.push_frame(frame, self.overlay(data, w, h))
        except Exception:
            log.debug("preview overlay failed", exc_info=True)

    def overlay(self, data, w, h):
        pos, bounds = data.get("pos"), data.get("bounds")
        dets, off = [], []
        for d in data.get("dets", []):
            dets.append([round(float(d[0]) / w, 4), round(float(d[1]) / h, 4), round(float(d[2]) / w, 4), round(float(d[3]) / h, 4)])
            o = False
            if pos is not None and bounds is not None:
                o = not bounds.contains(*pixel_to_angle((d[0] + d[2]) / 2, (d[1] + d[3]) / 2, pos, w, h))
            off.append(bool(o))
        stage = None
        if pos is not None and bounds is not None:
            stage = [[round(x / w, 4), round(y / h, 4)] for x, y in bounds.outline(pos, w, h)]
        box = data.get("box")
        return {
            "dets": dets,
            "off": off,
            "box": None if box is None else [round(float(box[0]) / w, 4), round(float(box[1]) / h, 4), round(float(box[2]) / w, 4), round(float(box[3]) / h, 4)],
            "lost": bool(data.get("lost")),
            "ignore": float(self.win.settings.ignore_below),
            "stage": stage,
        }

    # ---------------- commands in
    def on_command(self, cid, msg):
        w = self.win
        t = msg["t"]
        if t in rm.HOLD_KINDS and not self.watch_timer.isActive():
            self.watch_timer.start()
        if t == "joy":
            active = msg["x"] != 0 or msg["y"] != 0
            self.watch.touch(cid, "joy", active)
            if active:
                w.manual_press(f"remote:{cid}", msg["x"], msg["y"])
            elif f"remote:{cid}" in w.held:
                w.manual_release(f"remote:{cid}")
            return
        if t in ("zoom", "focus"):
            d = msg["d"]
            self.watch.touch(cid, t, d != 0)
            if d:
                self.owner[t] = cid
                if t == "zoom":
                    if w.zoom_held != d:
                        w.zoom_press(d)
                else:
                    w.cam.focus(0.5 * d)
            else:
                self._let_go(cid, t)
            return
        a = msg["a"]
        if a == "track":
            w.set_tracking(not w.track_btn.isChecked())
        elif a == "release":
            w.worker.release_target()
        elif a == "stop":
            self._release_everything()
            w.emergency_stop()
        elif a == "home":
            w.camera_home()
        elif a == "stage_shot":
            w.go_stage_home()
        elif a == "focus_once":
            w.cam.one_push_af()
        elif a == "focus_auto":
            w.cam.autofocus(True)
        elif a == "speed":
            w.step_speed(1 if msg["n"] > 0 else -1)  # the remote's Slower / Faster
        elif a == "auto_pick":
            w.auto_pick.setChecked(msg["on"])
        elif a == "pick":
            fw, fh = self._frame_size
            if fw and fh:
                w.worker.click(msg["x"] * fw, msg["y"] * fh)
        elif a == "preset":
            n, store = msg["n"], msg["store"]
            if store:
                if not self.cfg["allow_store"]:
                    return self.toast(cid, "Storing presets from a remote is turned off on the PC")
                if n == w.stage.home_preset:
                    return self.toast(cid, f"Preset {n} is the stage shot. Store it from the PC.")
                w.preset(n, store=True)
                self.toast(cid, f"Stored preset {n}")
            else:
                w.preset(n, store=False)
        self.push_state()

    def _let_go(self, cid, kind):
        """Stop a held control, unless another device has taken it over since."""
        if kind == "joy":
            if f"remote:{cid}" in self.win.held:
                self.win.manual_release(f"remote:{cid}")
            return
        if self.owner.get(kind) != cid:
            return
        self.owner.pop(kind, None)
        if kind == "zoom":
            self.win.zoom_release()
        else:
            self.win.cam.focus(0)

    def _check_holds(self):
        for cid, kind in self.watch.expired():
            log.info("remote %s went quiet holding %s: letting go", cid, kind)
            self._let_go(cid, kind)
        if not self.watch:
            self.watch_timer.stop()  # restarted by the next held control

    def on_left(self, cid):
        for _, kind in self.watch.drop(cid):
            self._let_go(cid, kind)
        for kind in ("zoom", "focus"):
            if self.owner.get(kind) == cid:
                self._let_go(cid, kind)
        if f"remote:{cid}" in self.win.held:
            self.win.manual_release(f"remote:{cid}")

    def _release_everything(self):
        for cid, kind in list(self.watch.seen):
            self._let_go(cid, kind)
        self.watch.seen.clear()

    def _paired(self, token, name):
        self.cfg["tokens"][token] = name
        self.win._save()
        self.win.statusBar().showMessage(f"Paired {name} for remote control")

    def _clients_changed(self):
        self.push_state()
        self._refresh_clients()

    # ---------------- settings tab
    def tab(self):
        from .widgets import label, setting

        w = QWidget()
        grid = QGridLayout(w)
        grid.setContentsMargins(4, 16, 4, 4)
        grid.setHorizontalSpacing(32)
        grid.setVerticalSpacing(14)

        left = QVBoxLayout()
        left.setSpacing(14)
        self.enable_check = QCheckBox("Allow phones and tablets to control the camera")
        self.enable_check.setChecked(self.cfg["enabled"])
        self.enable_check.toggled.connect(self.set_enabled)
        left.addWidget(setting("Remote control", "Only on this network, while PTZ Tracker is open. Off until you turn it on.", self.enable_check))

        self.url_label = label("", "cardTitle")
        self.url_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        left.addWidget(setting("Open on the tablet", "Then Add to Home Screen to use it like an app. Same Wi-Fi as this PC.", self.url_label))

        pin_row = QWidget()
        pr = QHBoxLayout(pin_row)
        pr.setContentsMargins(0, 0, 0, 0)
        self.pin_label = label("", "cardTitle")
        self.pin_label.setStyleSheet("color: #F5A524; letter-spacing: 4px;")
        new_pin = QPushButton("New PIN")
        new_pin.setToolTip("Devices already paired stay paired. Use Forget all to unpair them")
        new_pin.clicked.connect(self.new_pin)
        pr.addWidget(self.pin_label)
        pr.addWidget(new_pin)
        pr.addStretch(1)
        left.addWidget(setting("Pairing PIN", "Each device asks for it once, then remembers this PC", pin_row))

        self.preview_combo = QComboBox()
        for text, key in PREVIEW_CHOICES:
            self.preview_combo.addItem(text, key)
        self.preview_combo.setCurrentIndex([k for _, k in PREVIEW_CHOICES].index(self.cfg["preview"]))
        self.preview_combo.currentIndexChanged.connect(self._preview_changed)
        self.preview_combo.setFixedWidth(180)
        left.addWidget(setting("Video preview", "A small picture with the head boxes, for tapping on a head. Tracking runs here at full rate either way", self.preview_combo))

        self.store_check = QCheckBox("Let remotes store presets")
        self.store_check.setChecked(self.cfg["allow_store"])
        self.store_check.toggled.connect(self._store_changed)
        left.addWidget(setting("Presets", "The stage shot preset can only be stored from this PC", self.store_check))
        self.remote_status = label("", "hint", wrap=True)
        left.addWidget(self.remote_status)
        left.addStretch(1)

        right = QVBoxLayout()
        right.setSpacing(10)
        self.qr = QLabel()
        self.qr.setFixedSize(220, 220)
        self.qr.setAlignment(Qt.AlignCenter)
        right.addWidget(self.qr, 0, Qt.AlignHCenter)
        self.qr_hint = label("Scan with the tablet camera to open and pair in one go", "hint", wrap=True)
        self.qr_hint.setAlignment(Qt.AlignCenter)
        right.addWidget(self.qr_hint)
        right.addSpacing(8)
        right.addWidget(label("Connected", "section"))
        self.clients_box = QVBoxLayout()
        self.clients_box.setSpacing(6)
        right.addLayout(self.clients_box)
        forget = QPushButton("Forget all paired devices")
        forget.clicked.connect(self.forget_all)
        right.addWidget(forget, 0, Qt.AlignLeft)
        right.addStretch(1)

        grid.addLayout(left, 0, 0)
        grid.addLayout(right, 0, 1)
        grid.setColumnStretch(0, 1)
        self._refresh_tab()
        return w

    def url(self):
        return f"http://{rm.lan_ip()}:{self.cfg['port']}"

    def _refresh_tab(self):
        if not hasattr(self, "url_label"):
            return
        on = bool(self.server and self.server.running)
        url = self.url()
        self.url_label.setText(url if on else "Turn on remote control first")
        self.pin_label.setText(self.cfg["pin"])
        pm = qr_pixmap(f"{url}/#pin={self.cfg['pin']}", 200) if on else None
        self.qr.setPixmap(pm if pm else QPixmap())
        self.qr.setVisible(pm is not None)
        self.qr_hint.setVisible(pm is not None)
        if self.server and self.server.error:
            self.remote_status.setText(self.server.error)
        elif on and url.startswith("http://127."):
            self.remote_status.setText("This PC isn't on a network right now, so other devices can't reach it")
        elif on:
            self.remote_status.setText("On Windows, allow PTZ Tracker through the firewall (Private networks) if the tablet can't connect")
        else:
            self.remote_status.setText("")
        self._refresh_clients()

    def _refresh_clients(self):
        if not hasattr(self, "clients_box"):
            return
        from .widgets import label

        while self.clients_box.count():
            item = self.clients_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        clients = self.server.client_list() if self.server else []
        if not clients:
            self.clients_box.addWidget(label("No devices connected", "hint"))
            return
        for cid, name, active in clients:
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.addWidget(label(f"{'●' if active else '○'}  {name}"), 1)
            kick = QPushButton("Disconnect")
            kick.setObjectName("link")
            kick.clicked.connect(lambda _=False, c=cid: self.server and self.server.kick(c))
            h.addWidget(kick)
            self.clients_box.addWidget(row)

    def new_pin(self):
        self.cfg["pin"] = rm.new_pin()
        if self.server:
            self.server.set_pin(self.cfg["pin"])
        self.win._save()
        self._refresh_tab()

    def forget_all(self):
        self.cfg["tokens"] = {}
        if self.server:
            self.server.set_tokens({})
        self.win._save()
        self.win.statusBar().showMessage("Every paired device has to enter the PIN again")

    def _preview_changed(self, i):
        self.cfg["preview"] = self.preview_combo.itemData(i)
        if self.server:
            self.server.preview = self.cfg["preview"]
        self.push_state()

    def _store_changed(self, on):
        self.cfg["allow_store"] = bool(on)
        self.push_state()

    def tick(self):
        """Once a second from the window: keep the state fresh (camera connects, names) and the device list current."""
        self.push_state()
        if self.server and self.server.clients:
            self._refresh_clients()
