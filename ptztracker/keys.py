"""Key binds: the action list, defaults, and the key -> action lookup.

Keys are stored in config.json as QKeySequence portable strings ("A", "Left", "Esc"), so they read the same
on both OSes. Internally they are Qt key codes (ints). Shift is never part of a bind: Shift + a preset key stores.
"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence

HELD, PRESS = "held", "press"
SLOTS = 2  # primary + alternate key per action

# id, label, kind, default keys
ACTIONS = [
    ("pan_left", "Pan left", HELD, ["A", "Left"]),
    ("pan_right", "Pan right", HELD, ["D", "Right"]),
    ("tilt_up", "Tilt up", HELD, ["W", "Up"]),
    ("tilt_down", "Tilt down", HELD, ["S", "Down"]),
    ("zoom_in", "Zoom in", HELD, ["E", "+"]),
    ("zoom_out", "Zoom out", HELD, ["Q", "-"]),
    ("focus_near", "Focus near", HELD, ["["]),
    ("focus_far", "Focus far", HELD, ["]"]),
    ("track", "Start / stop tracking", PRESS, ["Space"]),
    ("release", "Release target", PRESS, ["Esc"]),
    ("stop", "Stop camera", PRESS, ["X"]),
    ("home", "Camera home", PRESS, ["H"]),
    ("stage_shot", "Go to stage shot", PRESS, ["G"]),
    ("focus_once", "Focus once", PRESS, ["F"]),
    ("speed_down", "Slower", PRESS, ["Z"]),
    ("speed_up", "Faster", PRESS, ["C"]),
    ("bank", "Next preset bank", PRESS, ["B"]),
] + [(f"preset_{i}", f"Recall preset {i}", PRESS, [str((i % 10))]) for i in range(1, 11)]  # fmt: skip

LABELS = {a: label for a, label, _, _ in ACTIONS}
KINDS = {a: kind for a, _, kind, _ in ACTIONS}
DEFAULTS = {a: list(keys) for a, _, _, keys in ACTIONS}
HELD_DIRS = {"pan_left": (-1, 0), "pan_right": (1, 0), "tilt_up": (0, 1), "tilt_down": (0, -1)}

# Shift + digit arrives as a symbol on most layouts; map it back to the digit key
UNSHIFT = {
    Qt.Key_Exclam: Qt.Key_1, Qt.Key_At: Qt.Key_2, Qt.Key_NumberSign: Qt.Key_3, Qt.Key_Dollar: Qt.Key_4,
    Qt.Key_Percent: Qt.Key_5, Qt.Key_AsciiCircum: Qt.Key_6, Qt.Key_Ampersand: Qt.Key_7,
    Qt.Key_Asterisk: Qt.Key_8, Qt.Key_ParenLeft: Qt.Key_9, Qt.Key_ParenRight: Qt.Key_0,
}  # fmt: skip
UNSHIFT = {int(k): int(v) for k, v in UNSHIFT.items()}
MODIFIER_KEYS = {int(k) for k in (Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt, Qt.Key_Meta, Qt.Key_AltGr, Qt.Key_CapsLock, Qt.Key_unknown)}
PRETTY = {"Left": "←", "Right": "→", "Up": "↑", "Down": "↓"}


def key_code(text):
    """'A' -> Qt key code, or None if it isn't a single valid key."""
    seq = QKeySequence(text, QKeySequence.PortableText)
    if seq.count() != 1:
        return None
    k = int(seq[0].key())
    return k if k and k not in MODIFIER_KEYS else None


def key_text(code):
    return QKeySequence(code).toString(QKeySequence.PortableText)


def pretty(code):
    t = key_text(code)
    return PRETTY.get(t, t)


class Binds:
    """action id -> up to SLOTS key codes (0 = empty slot). No key is ever bound to two actions."""

    def __init__(self, cfg_keys=None):
        self.keys = {}
        self.load(cfg_keys)

    @staticmethod
    def _codes(texts):
        codes = [key_code(t) if isinstance(t, str) else None for t in (texts or [])][:SLOTS]
        return (codes + [None] * SLOTS)[:SLOTS]

    def load(self, cfg_keys):
        """Merge a saved dict over the defaults. Actions missing from it (new in this version) get their default
        keys, unless a saved bind already uses that key."""
        saved = cfg_keys if isinstance(cfg_keys, dict) else {}
        self.keys, used = {}, set()
        for action in DEFAULTS:
            if action in saved and isinstance(saved[action], list):
                codes = self._codes(saved[action])
                codes = [c if c and c not in used else None for c in codes]
                used.update(c for c in codes if c)
                self.keys[action] = codes
        for action, texts in DEFAULTS.items():
            if action not in self.keys:
                codes = [c if c and c not in used else None for c in self._codes(texts)]
                used.update(c for c in codes if c)
                self.keys[action] = codes

    def to_cfg(self):
        return {a: [key_text(c) for c in codes if c] for a, codes in self.keys.items()}

    def owner(self, code):
        for a, codes in self.keys.items():
            if code in codes:
                return a
        return None

    def assign(self, action, slot, code):
        """Binds code to the action's slot. Returns the action that lost the key, if any."""
        taken = self.owner(code)
        if taken and not (taken == action and self.keys[action][slot] == code):
            self.keys[taken] = [None if c == code else c for c in self.keys[taken]]
        else:
            taken = None
        self.keys[action][slot] = code
        return taken if taken != action else None

    def clear(self, action, slot):
        self.keys[action][slot] = None

    def reset(self, action):
        """Back to the default keys. Returns the actions that lost a key to it."""
        lost = []
        self.keys[action] = [None] * SLOTS
        for i, code in enumerate(c for c in self._codes(DEFAULTS[action]) if c):
            other = self.owner(code)
            if other:
                lost.append(other)
                self.keys[other] = [None if c == code else c for c in self.keys[other]]
            self.keys[action][i] = code
        return lost

    def reset_all(self):
        self.load(None)

    def map(self):
        return {c: a for a, codes in self.keys.items() for c in codes if c}

    def resolve(self, key, shift=False, table=None):
        """Key press -> (action, store). store is True for Shift + a preset key."""
        table = self.map() if table is None else table
        key = int(key)
        if shift:
            action = table.get(UNSHIFT.get(key, key))
            if action and action.startswith("preset_"):
                return action, True
        action = table.get(key)
        if action is None and key == int(Qt.Key_Equal):  # the + key without Shift
            action = table.get(int(Qt.Key_Plus))
        return action, False

    def short(self, action):
        """Primary key as shown on buttons and tooltips ('' if unbound)."""
        for c in self.keys[action]:
            if c:
                return pretty(c)
        return ""

    def all_text(self, action):
        return " / ".join(pretty(c) for c in self.keys[action] if c)


def preset_slot(action, bank=0):
    """'preset_3' -> 3 (+10 in bank 2); None for other actions."""
    if action.startswith("preset_"):
        return int(action[7:]) + 10 * bank
    return None
