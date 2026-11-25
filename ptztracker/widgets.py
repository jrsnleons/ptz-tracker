"""The look (colours, fonts, stylesheet) and the small widget helpers shared by every window."""
import logging
import os

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from . import keys, platform_paths

log = logging.getLogger("ptztracker")


INK, INK2, INK3 = "#EDEEF0", "#A3A7AD", "#8A8F96"
GROUND, PANEL, RAISED, LINE = "#121314", "#1B1C1E", "#242629", "#303236"
GREEN, RED, AMBER = "#3DD68C", "#F2555A", "#F5A524"
if platform_paths.IS_WIN:
    UI_FONT, MONO_FONT, MOD = "Segoe UI Variable Text", "Cascadia Mono", "Ctrl"
else:
    UI_FONT, MONO_FONT, MOD = ".AppleSystemUIFont", "Menlo", "⌘"

STYLE = f"""
QWidget {{ color: {INK}; font-size: 13px; font-family: "{UI_FONT}"; }}
QMainWindow, #central {{ background: {GROUND}; }}
#topbar {{ background: {PANEL}; border-bottom: 1px solid {LINE}; }}
#rail {{ background: {PANEL}; }}
#railScroll {{ background: {PANEL}; border: 0; border-left: 1px solid {LINE}; }}
QStatusBar {{ background: {PANEL}; border-top: 1px solid {LINE}; color: {INK2}; font-size: 11px; }}
QStatusBar QLabel {{ color: {INK2}; font-size: 11px; padding-right: 12px; }}
QPushButton {{ background: {RAISED}; border: 1px solid {LINE}; border-radius: 8px; padding: 6px 12px; min-height: 18px; }}
QPushButton:hover {{ background: #2C2E32; }}
QPushButton:pressed {{ background: #34373B; }}
QPushButton:disabled {{ color: #5A5D63; }}
QPushButton#track {{ border: 0; border-radius: 10px; padding: 0; min-height: 66px; max-height: 66px; }}
QPushButton#track:checked {{ background: {GREEN}; }}
QCheckBox:disabled, QLabel:disabled {{ color: #5A5D63; }}
QPushButton#stop {{ background: transparent; border: 2px solid {RED}; color: {RED}; border-radius: 10px; font-size: 15px; font-weight: 700; }}
QPushButton#stop:hover {{ background: rgba(242, 85, 90, 0.14); }}
QPushButton#primary {{ background: {INK}; color: {GROUND}; border: 0; border-radius: 10px; font-size: 15px; font-weight: 700; padding: 10px; }}
QPushButton#primary:hover {{ background: #FFFFFF; }}
QPushButton#store {{ background: transparent; border: 1px dashed #5A5D63; color: {INK2}; }}
QPushButton#store:checked {{ background: {AMBER}; border: 1px solid {AMBER}; color: #2A1A00; font-weight: 700; }}
QPushButton#preset {{ font-size: 14px; font-weight: 600; padding: 4px 2px; }}
QPushButton#seg {{ padding: 4px 0; border-radius: 6px; font-weight: 600; }}
QPushButton#seg:checked {{ background: {INK}; color: {GROUND}; border: 0; }}
QPushButton#keybtn {{ min-width: 84px; padding: 4px 8px; }}
QPushButton#keybtn:checked {{ border: 1px solid {AMBER}; color: {AMBER}; }}
QFrame#updatebar {{ background: #1F2A3D; border-bottom: 1px solid {LINE}; }}
QProgressBar {{ background: {RAISED}; border: 0; border-radius: 4px; max-height: 8px; }}
QProgressBar::chunk {{ background: {GREEN}; border-radius: 4px; }}
QPushButton#link {{ background: transparent; border: 0; color: {INK2}; padding: 2px 4px; font-size: 12px; }}
QPushButton#link:hover {{ color: {INK}; }}
QPushButton#link:checked {{ color: {AMBER}; font-weight: 600; }}
QPushButton#link:disabled, QPushButton#store:disabled, QLabel#muted:disabled {{ color: #5A5D63; }}
QPushButton#chip {{ font-size: 12px; padding: 4px 12px; }}
QLabel#section {{ color: {INK2}; font-size: 12px; font-weight: 600; }}
QLabel#hint {{ color: {INK3}; font-size: 11px; }}
QLabel#muted {{ color: {INK2}; }}
QLabel#perf {{ color: {INK2}; font-family: "{MONO_FONT}", Consolas, monospace; font-size: 12px; }}
QLabel#cardTitle {{ font-size: 18px; font-weight: 700; }}
QFrame#card {{ background: {PANEL}; border: 1px solid {LINE}; border-radius: 12px; }}
QCheckBox {{ spacing: 8px; }}
QDialog {{ background: {PANEL}; }}
QTabWidget::pane {{ border: 0; }}
QTabBar::tab {{ background: transparent; color: {INK2}; padding: 6px 16px; border-radius: 6px; margin-right: 4px; }}
QTabBar::tab:selected {{ background: {LINE}; color: {INK}; font-weight: 600; }}
"""


def dark_palette():
    pal = QPalette()
    for role, color in (
        (QPalette.Window, PANEL), (QPalette.WindowText, INK), (QPalette.Base, RAISED), (QPalette.AlternateBase, PANEL),
        (QPalette.Text, INK), (QPalette.Button, RAISED), (QPalette.ButtonText, INK), (QPalette.ToolTipBase, RAISED),
        (QPalette.ToolTipText, INK), (QPalette.Highlight, "#4C8BF5"), (QPalette.HighlightedText, "#FFFFFF"),
        (QPalette.PlaceholderText, INK3), (QPalette.Mid, LINE), (QPalette.Dark, GROUND), (QPalette.Light, "#3A3D42"),
    ):  # fmt: skip
        pal.setColor(role, QColor(color))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor("#5A5D63"))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#5A5D63"))
    pal.setColor(QPalette.Disabled, QPalette.WindowText, QColor("#5A5D63"))
    return pal


def resource_path(*parts):
    """Files shipped with the app: repo root from source, Contents/Resources in the .app, _internal on Windows."""
    return os.path.join(platform_paths.resource_dir(), *parts)


def dot_icon(color):
    pm = QPixmap(16, 16)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.NoPen)
    p.drawEllipse(4, 4, 8, 8)
    p.end()
    return QIcon(pm)


# --------------------------------------------------------------------------- helpers


def label(text, name=None, wrap=False):
    lbl = QLabel(text)
    if name:
        lbl.setObjectName(name)
    lbl.setWordWrap(wrap)
    return lbl


def nofocus(b):
    b.setFocusPolicy(Qt.NoFocus)
    return b


def hold_button(text, on_press, on_release, w=40, h=40, tip=None):
    b = nofocus(QPushButton(text))
    b.setFixedSize(w, h)
    b.pressed.connect(on_press)
    b.released.connect(on_release)
    if tip:
        b.setToolTip(tip)
    return b


def slider(minv, maxv, value, on_change, scale=100.0, fmt=None):
    """Slider with its value at the right; the row exposes .slider for resets and .set(value) to move it silently.
    fmt turns the value into the text shown at the right."""
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    s = QSlider(Qt.Horizontal)
    s.setRange(round(minv * scale), round(maxv * scale))
    s.setValue(round(value * scale))
    s.setFocusPolicy(Qt.NoFocus)
    s.setMinimumWidth(120)
    text = fmt or (lambda x: f"{x:.2f}")
    lbl = label(text(value), "muted")
    lbl.setFixedWidth(36 if fmt is None else 54)
    lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def changed(v):
        lbl.setText(text(v / scale))
        on_change(v / scale)

    def set_value(x):
        s.blockSignals(True)
        s.setValue(round(x * scale))
        s.blockSignals(False)
        lbl.setText(text(x))

    s.valueChanged.connect(changed)
    lay.addWidget(s)
    lay.addWidget(lbl)
    row.slider, row.scale, row.set = s, scale, set_value
    return row


def setting(title, help_text, control):
    """A labelled setting: title, the control, then a one-line explanation."""
    box = QWidget()
    v = QVBoxLayout(box)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(3)
    v.addWidget(label(title))
    v.addWidget(control)
    v.addWidget(label(help_text, "hint", wrap=True))
    return box


def section(title, *right):
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(label(title, "section"))
    row.addStretch(1)
    for w in right:
        row.addWidget(w)
    return row


class KeyButton(QPushButton):
    """Shows a key; click it, then press the new key. Esc cancels, Backspace/Delete clears."""

    captured = Signal(int)
    cleared = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("keybtn")
        self.setCheckable(True)
        self.shown = ""
        self.toggled.connect(self._toggled)

    def set_key(self, text):
        self.shown = text
        if not self.isChecked():
            self.setText(text or "—")

    def _toggled(self, on):
        if on:
            self.setText("Press a key…")
            self.setFocus()
        else:
            self.setText(self.shown or "—")

    def event(self, e):
        if self.isChecked() and e.type() == QEvent.KeyPress:  # also takes Tab and Space while listening
            k = int(e.key())
            if k in keys.MODIFIER_KEYS:
                return True
            self.setChecked(False)
            if k in (int(Qt.Key_Backspace), int(Qt.Key_Delete)):
                self.cleared.emit()
            elif k != int(Qt.Key_Escape):
                self.captured.emit(k)
            return True
        return super().event(e)

    def focusOutEvent(self, e):
        self.setChecked(False)
        super().focusOutEvent(e)


class TrackButton(QPushButton):
    """The big start/stop button: a title, a line under it, and the Space key hint."""

    def __init__(self):
        super().__init__()
        self.setObjectName("track")
        self.setCheckable(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedHeight(68)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 8, 18, 8)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.title = label("")
        self.sub = label("")
        text.addWidget(self.title)
        text.addWidget(self.sub)
        lay.addLayout(text, 1)
        self.key = label("Space")
        lay.addWidget(self.key, 0, Qt.AlignVCenter)
        for w in (self.title, self.sub, self.key):
            w.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.toggled.connect(lambda _: self._paint_text())

    def set_text(self, title, sub):
        self.title.setText(title)
        self.sub.setText(sub)
        self._paint_text()

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == QEvent.EnabledChange:
            self._paint_text()

    def _paint_text(self):
        color = "#5A5D63" if not self.isEnabled() else "#0B2717" if self.isChecked() else INK
        base = f"color: {color}; background: transparent;"
        self.title.setStyleSheet(base + "font-size: 17px; font-weight: 700;")
        self.sub.setStyleSheet(base + "font-size: 12px;")
        self.key.setStyleSheet(base + f"font-size: 11px; font-weight: 600; border: 1px solid {color}; border-radius: 4px; padding: 2px 6px;")
