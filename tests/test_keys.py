"""Key binds: defaults, config round trip, conflicts, old configs, Shift+digit store, preset banks.

    .venv/bin/python -m tests.test_keys
"""
import sys

from PySide6.QtCore import Qt

from ptztracker import keys

ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(("PASS" if cond else "FAIL") + f"  {name}" + (f" ({detail})" if detail and not cond else ""))


def code(text):
    return keys.key_code(text)


def main():
    b = keys.Binds()
    m = b.map()
    check("defaults: WASD and arrows", all(m[code(k)] == a for k, a in (("A", "pan_left"), ("Left", "pan_left"), ("D", "pan_right"), ("Right", "pan_right"), ("W", "tilt_up"), ("Up", "tilt_up"), ("S", "tilt_down"), ("Down", "tilt_down"))))
    check("defaults: zoom, focus, controls", all(m[code(k)] == a for k, a in (("E", "zoom_in"), ("+", "zoom_in"), ("Q", "zoom_out"), ("-", "zoom_out"), ("[", "focus_near"), ("]", "focus_far"), ("Space", "track"), ("Esc", "release"), ("X", "stop"), ("H", "home"), ("G", "stage_shot"), ("F", "focus_once"), ("Z", "speed_down"), ("C", "speed_up"), ("B", "bank"))))
    check("defaults: 1-9 and 0 are presets 1-10", all(m[code(str(i % 10))] == f"preset_{i}" for i in range(1, 11)))
    check("no key is bound twice by default", sum(len([c for c in v if c]) for v in b.keys.values()) == len(m))
    check("every action has a label and a kind", all(a in keys.LABELS and keys.KINDS[a] in (keys.HELD, keys.PRESS) for a in b.keys))

    # config round trip
    b.assign("zoom_in", 0, code("R"))
    cfg = b.to_cfg()
    again = keys.Binds(cfg)
    check("round trip through the config dict", again.keys == b.keys and again.to_cfg() == cfg, cfg["zoom_in"])
    check("config values are portable strings", cfg["zoom_in"] == ["R", "+"] and cfg["release"] == ["Esc"], cfg["release"])

    # conflict: a key lives on one action only
    b = keys.Binds()
    lost = b.assign("pan_left", 0, code("E"))  # E was zoom in
    check("assigning a used key reports who lost it", lost == "zoom_in", lost)
    check("the loser is cleared, the new owner has it", b.keys["zoom_in"][0] is None and b.map()[code("E")] == "pan_left")
    check("the loser keeps its other key", b.keys["zoom_in"][1] == code("+"))
    check("same key in the same slot is not a conflict", b.assign("pan_left", 0, code("E")) is None)
    b.assign("pan_left", 1, code("E"))  # same action, other slot
    check("moving a key between slots of one action leaves one copy", b.keys["pan_left"] == [None, code("E")])
    lost = b.reset("zoom_in")
    check("reset takes the default keys back", b.keys["zoom_in"] == [code("E"), code("+")] and lost == ["pan_left"], (b.keys["zoom_in"], lost))
    b.reset_all()
    check("restore defaults", b.to_cfg() == keys.Binds().to_cfg())

    # old configs
    check("config without 'keys' gives the defaults", keys.Binds(None).to_cfg() == keys.Binds({}).to_cfg() == keys.Binds().to_cfg())
    junk = keys.Binds({"pan_left": "A", "stop": ["???", 5], "zoom_in": ["Ctrl+Shift+Z"], "nope": ["A"]})
    check("junk in the config falls back safely", junk.keys["pan_left"] == [code("A"), code("Left")] and junk.keys["stop"] == [None, None] and "nope" not in junk.keys)
    old = keys.Binds({"pan_left": ["A", "Left"], "speed_up": [], "track": ["C"]})  # user put C on tracking; 'speed_up' emptied
    check("a saved bind wins over a new action's default key", old.map()[code("C")] == "track" and old.keys["speed_up"] == [None, None])
    old = keys.Binds({"track": ["Z"]})  # Z is the default for speed_down, which this old config never heard of
    check("a new action does not steal a key the user already uses", old.map()[code("Z")] == "track" and old.keys["speed_down"] == [None, None])

    # resolving key presses
    b = keys.Binds()
    shift_1 = b.resolve(int(Qt.Key_Exclam), shift=True)
    check("Shift+1 arrives as '!' and stores preset 1", shift_1 == ("preset_1", True), shift_1)
    check("Shift+0 arrives as ')' and stores preset 10", b.resolve(int(Qt.Key_ParenRight), shift=True) == ("preset_10", True))
    check("plain 3 recalls preset 3", b.resolve(int(Qt.Key_3)) == ("preset_3", False))
    check("Shift+3 as the digit itself also stores", b.resolve(int(Qt.Key_3), shift=True) == ("preset_3", True))
    check("Shift+W still moves, it does not store", b.resolve(int(Qt.Key_W), shift=True) == ("tilt_up", False))
    check("= works as + for zoom in", b.resolve(int(Qt.Key_Equal)) == ("zoom_in", False))
    check("unbound key resolves to nothing", b.resolve(int(Qt.Key_J)) == (None, False))
    b.assign("preset_3", 0, code("J"))
    check("rebound preset key recalls and Shift+key stores", b.resolve(int(Qt.Key_J)) == ("preset_3", False) and b.resolve(int(Qt.Key_J), shift=True) == ("preset_3", True))

    # preset banks: bank 2 shifts slots by 10
    check("bank 1 slots", [keys.preset_slot(f"preset_{i}", 0) for i in (1, 5, 10)] == [1, 5, 10])
    check("bank 2 slots", [keys.preset_slot(f"preset_{i}", 1) for i in (1, 5, 10)] == [11, 15, 20])
    check("non-preset action has no slot", keys.preset_slot("stop") is None)
    print("all passed" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
