"""Writes latest.json for a release: python packaging/make_latest.py <folder with the built files> [notes.md]

The in-app updater reads it (ptztracker/updater.py). File names come from what is in the folder; each gets a sha256.
"""
import hashlib
import json
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ASSETS = {"windows": "PTZ-Tracker-Setup.exe", "windows_zip": "PTZ-Tracker-windows.zip", "mac": "PTZ-Tracker-mac.zip"}


def version():
    ns = {}
    exec(open(os.path.join(ROOT, "ptztracker", "version.py"), encoding="utf-8").read(), ns)
    return ns["__version__"]


def whats_new(text):
    """The '### What's new' section of the release notes (the whole text if there isn't one)."""
    m = re.search(r"^###\s+What's new\s*\n(.*?)(?=^###\s|\Z)", text, re.S | re.M)
    return (m.group(1) if m else text).strip()


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build(folder, notes_path):
    missing = [n for n in ASSETS.values() if not os.path.exists(os.path.join(folder, n))]
    if missing:
        raise SystemExit(f"missing from {folder}: {', '.join(missing)}")
    with open(notes_path, encoding="utf-8") as f:
        notes = whats_new(f.read())
    return {
        "version": version(),
        "notes": notes,
        "assets": dict(ASSETS),
        "sha256": {name: sha256(os.path.join(folder, name)) for name in ASSETS.values()},
    }


if __name__ == "__main__":
    folder = sys.argv[1]
    data = build(folder, sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "packaging", "RELEASE_NOTES.md"))
    with open(os.path.join(folder, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(json.dumps({**data, "notes": data["notes"][:80] + "…"}, indent=2))
