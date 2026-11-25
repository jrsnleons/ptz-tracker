"""Picture settings read from the camera itself (Canon web API), for the main window, like Canon's own app.

Every control shown is one the camera reports in its info list: c.1.<key> with either a .list of choices (a drop-down)
or a .min / .max range (a slider). Values start from what the camera says and follow it when it changes (from another
app, or a preset), except while you are dragging. Changes go out as control?<key>=<value>; the client keeps only the
newest value per key, so dragging can't flood the camera or arrive out of order.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QGridLayout, QPushButton, QSlider, QVBoxLayout, QWidget

from .widgets import label, nofocus


def _scaled(div, unit="", sign=False):
    def text(v):
        x = int(v) / div
        return (f"{x:+g}" if sign else f"{x:g}") + unit

    return text


# control key -> (title, raw value -> text). Order is the order shown. Scales are RCCA's (unconfirmed on the CR-N300).
CONTROLS = {
    "exp": ("Exposure", str),
    "me.brightness": ("AE level", _scaled(4, sign=True)),
    "me.diaphragm": ("Iris", lambda v: f"F{int(v) / 100:g}"),
    "me.gain": ("Gain", _scaled(10, " dB")),
    "me.shutter.mode": ("Shutter mode", str),
    "me.shutter": ("Shutter", str),
    "nd.filter": ("ND", str),
    "wb": ("White balance", str),
    "wb.kelvin": ("Color temp", lambda v: f"{v} K"),
    "ac": ("Sharpness", str),
    "blacklevel": ("Black level", str),
    "knee": ("Knee", str),
}


def _text(fmt, v):
    try:
        return fmt(v)
    except (TypeError, ValueError):
        return str(v)


def available(state):
    """key -> ("list", [choices]) or ("range", lo, hi) for each control the camera reports."""
    out = {}
    for key in CONTROLS:
        base = f"c.1.{key}"
        if base not in state:
            continue
        if state.get(base + ".list"):
            out[key] = ("list", [c.strip() for c in state[base + ".list"].split(",") if c.strip()])
        else:
            try:
                out[key] = ("range", int(float(state[base + ".min"])), int(float(state[base + ".max"])))
            except (KeyError, ValueError):
                pass
    return out


class CanonPicturePanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.api = None  # canon_http.CanonAPI
        self._shape = None
        self._rows = {}  # key -> widget
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        self.note = label("Connect with the Camera IP to change the picture here.", "hint", wrap=True)
        v.addWidget(self.note)
        self.grid_box = QWidget()
        self.grid = QGridLayout(self.grid_box)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(8)
        self.grid.setVerticalSpacing(4)
        self.grid.setColumnStretch(1, 1)
        v.addWidget(self.grid_box)
        self.save_btn = nofocus(QPushButton("Save camera settings"))
        self.save_btn.setToolTip("Asks the camera to keep these picture settings (Canon's Save Camera Settings)")
        self.save_btn.clicked.connect(lambda: self.api and self.api.save_settings())
        v.addWidget(self.save_btn, 0, Qt.AlignLeft)
        self.save_btn.hide()

    def set_api(self, api):
        self.api = api
        if api is None:
            self.update_state({})

    def update_state(self, state):
        """New info list from the camera (GUI thread). Rebuilds the controls only when what the camera offers changes."""
        avail = available(state) if self.api else {}
        shape = tuple((k, a[0], tuple(a[1:])) for k, a in avail.items())
        if shape != self._shape:
            self._build(avail)
            self._shape = shape
        for key, w in self._rows.items():
            self._show(w, state.get(f"c.1.{key}", ""))
        self.note.setVisible(not avail)
        if self.api and not avail:
            self.note.setText("Waiting for the camera's picture settings…" if not state else "This camera reports no picture settings over its web API.")
        elif not self.api:
            self.note.setText("Connect with the Camera IP to change the picture here.")
        self.save_btn.setVisible(bool(avail))

    def _build(self, avail):
        for w in self.grid_box.findChildren(QWidget):
            w.deleteLater()
        self._rows = {}
        for r, (key, a) in enumerate(avail.items()):
            title, fmt = CONTROLS[key]
            self.grid.addWidget(label(title, "muted"), r, 0)
            if a[0] == "list":
                w = QComboBox()
                w.setFocusPolicy(Qt.NoFocus)
                for c in a[1]:
                    w.addItem(_text(fmt, c), c)
                w.activated.connect(lambda i, k=key, w=w: self._send(k, w.itemData(i)))
                self.grid.addWidget(w, r, 1, 1, 2)
            else:
                w = QSlider(Qt.Horizontal)
                w.setRange(a[1], a[2])
                w.setFocusPolicy(Qt.NoFocus)
                val = label("", "muted")
                val.setFixedWidth(58)
                val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                w.value_label, w.fmt = val, fmt
                w.valueChanged.connect(lambda x, k=key, w=w, f=fmt: (w.value_label.setText(_text(f, x)), self._send(k, x)))
                self.grid.addWidget(w, r, 1)
                self.grid.addWidget(val, r, 2)
            self._rows[key] = w

    def _show(self, w, value):
        if isinstance(w, QComboBox):
            i = w.findData(value)
            if i >= 0 and not w.view().isVisible():
                w.setCurrentIndex(i)
        elif not w.isSliderDown():
            try:
                x = int(float(value))
            except ValueError:
                return
            w.blockSignals(True)
            w.setValue(x)
            w.blockSignals(False)
            w.value_label.setText(_text(w.fmt, x))

    def _send(self, key, value):
        if self.api is not None:
            self.api.set(key, value)
