"""Updater logic that needs no network: version compare, latest.json parsing, asset pick, sha256, mac bundle path.

    .venv/bin/python -m tests.test_updater
"""
import json
import os
import sys
import tempfile

from ptztracker import updater
from ptztracker.version import __version__

ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(("PASS" if cond else "FAIL") + f"  {name}" + (f" ({detail})" if detail and not cond else ""))


LATEST = {
    "version": "1.2.1",
    "notes": "Fixes things.",
    "assets": {"windows": "PTZ-Tracker-Setup.exe", "windows_zip": "PTZ-Tracker-windows.zip", "mac": "PTZ-Tracker-mac.zip"},
    "sha256": {"PTZ-Tracker-Setup.exe": "AA" * 32, "PTZ-Tracker-windows.zip": "bb" * 32, "PTZ-Tracker-mac.zip": "cc" * 32},
}


def main():
    check("version.py has a dotted version", updater.parse_version(__version__) >= (1, 2, 0), __version__)
    check("parse 1.2.10", updater.parse_version("1.2.10") == (1, 2, 10))
    check("parse v-prefix", updater.parse_version("v1.2.0") == (1, 2, 0))
    check("parse garbage is (0,)", updater.parse_version("banana") == (0,))
    check("1.2.10 is newer than 1.2.9 (numeric, not text)", updater.is_newer("1.2.10", "1.2.9"))
    check("1.10.0 is newer than 1.9.9", updater.is_newer("1.10.0", "1.9.9"))
    check("same version is not newer", not updater.is_newer("1.2.0", "1.2.0"))
    check("1.2 equals 1.2.0", not updater.is_newer("1.2", "1.2.0") and not updater.is_newer("1.2.0", "1.2"))
    check("older is not newer", not updater.is_newer("1.1.3", "1.2.0"))
    check("1.2.0.1 is newer than 1.2.0", updater.is_newer("1.2.0.1", "1.2.0"))
    check("garbage is never newer", not updater.is_newer("banana", "1.2.0"))

    rel = updater.parse_latest(json.dumps(LATEST))
    check("latest.json parses", rel and rel.version == "1.2.1" and rel.notes == "Fixes things." and rel.assets["mac"] == "PTZ-Tracker-mac.zip")
    check("parses from bytes and dict too", updater.parse_latest(json.dumps(LATEST).encode()).version == "1.2.1" and updater.parse_latest(LATEST).version == "1.2.1")
    check("hashes are lower-cased", rel.sha256["PTZ-Tracker-Setup.exe"] == "aa" * 32)
    check("v-prefixed version is accepted", updater.parse_latest({"version": "v1.3.0"}).version == "1.3.0")
    check("missing version -> None", updater.parse_latest({"assets": {}}) is None)
    check("not json -> None", updater.parse_latest("<html>") is None and updater.parse_latest(b"") is None and updater.parse_latest("[]") is None)
    check("odd version -> None", updater.parse_latest({"version": "latest"}) is None)
    bad = updater.parse_latest({"version": "1.2.1", "assets": {"windows": "../evil.exe", "mac": "ok.zip", "x": 5}})
    check("asset names with path parts are dropped", bad.assets == {"mac": "ok.zip"}, bad.assets)
    check("download URL points at the tagged release", rel.url("PTZ-Tracker-mac.zip") == "https://github.com/jrsnleons/ptz-tracker/releases/download/v1.2.1/PTZ-Tracker-mac.zip")

    check("windows pick", updater.pick_asset(rel, "windows") == ("PTZ-Tracker-Setup.exe", "aa" * 32))
    check("mac pick", updater.pick_asset(rel, "mac") == ("PTZ-Tracker-mac.zip", "cc" * 32))
    check("no asset for a platform", updater.pick_asset(updater.parse_latest({"version": "1.2.1", "assets": {"mac": "m.zip"}}), "windows") == (None, None))
    check("unknown platform", updater.pick_asset(rel, "linux") == (None, None))
    check("asset without a hash gives None for it", updater.pick_asset(updater.parse_latest({"version": "1.2.1", "assets": {"mac": "m.zip"}}), "mac") == ("m.zip", None))

    d = tempfile.mkdtemp()
    f = os.path.join(d, "x.bin")
    with open(f, "wb") as fh:
        fh.write(b"hello")
    want = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    check("sha256 of a file", updater.sha256_file(f) == want)
    check("verify accepts the right hash (any case)", updater.verify_sha256(f, want) and updater.verify_sha256(f, want.upper()))
    check("verify rejects a wrong hash", not updater.verify_sha256(f, "0" * 64))
    check("verify rejects when no hash is given", not updater.verify_sha256(f, None) and not updater.verify_sha256(f, ""))

    check("mac bundle from the executable path", updater.mac_bundle("/Applications/PTZ Tracker.app/Contents/MacOS/PTZ Tracker") == "/Applications/PTZ Tracker.app")
    check("no bundle when run from source", updater.mac_bundle("/usr/local/bin/python3") is None)
    check("from source the app does not self-update", not updater.can_self_update())
    check("mac helper waits for the pid and clears quarantine", "kill -0" in updater.MAC_HELPER and "com.apple.quarantine" in updater.MAC_HELPER and "open" in updater.MAC_HELPER)
    release_files()
    print("all passed" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)


def release_files():
    """packaging/make_latest.py writes a latest.json the updater accepts."""
    import importlib.util

    root = os.path.join(os.path.dirname(__file__), "..")
    spec = importlib.util.spec_from_file_location("make_latest", os.path.join(root, "packaging", "make_latest.py"))
    ml = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ml)
    d = tempfile.mkdtemp()
    for name in ml.ASSETS.values():
        with open(os.path.join(d, name), "wb") as f:
            f.write(name.encode())
    data = ml.build(d, os.path.join(root, "packaging", "RELEASE_NOTES.md"))
    rel = updater.parse_latest(json.dumps(data))
    check("make_latest output parses", rel and rel.version == __version__ and set(rel.assets) == {"windows", "windows_zip", "mac"})
    name, sha = updater.pick_asset(rel, "mac")
    check("and the hash it wrote verifies the file", updater.verify_sha256(os.path.join(d, name), sha))
    check("notes are the What's new section", rel.notes.startswith("- ") and "###" not in rel.notes)


if __name__ == "__main__":
    main()
