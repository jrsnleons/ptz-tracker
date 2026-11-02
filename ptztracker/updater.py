"""Self-update: find a newer release, download it, check its hash, and hand over to the installer.

No Qt in here, so the parsing and picking logic is testable without a window or a network.
"""
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field

from . import platform_paths

log = logging.getLogger(__name__)

REPO = "jrsnleons/ptz-tracker"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
LATEST_URL = f"https://github.com/{REPO}/releases/latest/download/latest.json"
WIN_FLAGS = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/CLOSEAPPLICATIONS"]

MAC_HELPER = """#!/bin/sh
# usage: helper.sh <pid of the running app> <new .app> <installed .app>
while kill -0 "$1" 2>/dev/null; do sleep 0.3; done
sleep 0.5
OLD="$3.old.$$"
if mv "$3" "$OLD" && mv "$2" "$3"; then
  rm -rf "$OLD"
else
  [ -e "$OLD" ] && [ ! -e "$3" ] && mv "$OLD" "$3"
fi
xattr -dr com.apple.quarantine "$3" 2>/dev/null
open "$3"
"""


def parse_version(text):
    """'v1.2.10' -> (1, 2, 10). Anything unparseable is (0,) so it never looks newer."""
    nums = re.findall(r"\d+", str(text).split("-")[0])
    return tuple(int(n) for n in nums) if nums else (0,)


def is_newer(candidate, current):
    a, b = parse_version(candidate), parse_version(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


@dataclass
class Release:
    version: str
    notes: str = ""
    assets: dict = field(default_factory=dict)  # "windows" | "windows_zip" | "mac" -> file name
    sha256: dict = field(default_factory=dict)  # file name -> hex digest

    @property
    def page(self):
        return f"https://github.com/{REPO}/releases/tag/v{self.version}"

    def url(self, name):
        return f"https://github.com/{REPO}/releases/download/v{self.version}/{name}"


def parse_latest(data):
    """latest.json (text, bytes or dict) -> Release, or None if it isn't a usable one."""
    try:
        d = json.loads(data) if isinstance(data, (str, bytes)) else data
        version = str(d["version"]).lstrip("v").strip()
        if not re.fullmatch(r"\d+(\.\d+)*", version):
            return None
        assets = {k: v for k, v in (d.get("assets") or {}).items() if isinstance(v, str) and v and "/" not in v and "\\" not in v}
        sha = {k: str(v).lower() for k, v in (d.get("sha256") or {}).items()}
        return Release(version, str(d.get("notes") or ""), assets, sha)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def platform_key():
    return "windows" if platform_paths.IS_WIN else "mac" if platform_paths.IS_MAC else None


def pick_asset(release, key=None):
    """(file name, sha256 or None) for this platform, or (None, None)."""
    key = key or platform_key()
    name = release.assets.get(key)
    if not name:
        return None, None
    return name, release.sha256.get(name) or release.sha256.get(key)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_sha256(path, expected):
    """True only when a hash was given and the file matches it. A release without one is not trusted."""
    return bool(expected) and sha256_file(path) == expected.lower()


def fetch_latest(url=LATEST_URL, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "PTZ-Tracker-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return parse_latest(r.read(1 << 20))


def download(url, dest, progress=None, timeout=20):
    """Downloads to dest, calling progress(fraction 0..1) as it goes."""
    req = urllib.request.Request(url, headers={"User-Agent": "PTZ-Tracker-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if progress and total:
                progress(min(1.0, got / total))
    return dest


def mac_bundle(executable=None):
    """.../PTZ Tracker.app from .../PTZ Tracker.app/Contents/MacOS/PTZ Tracker, or None."""
    p = os.path.abspath(executable or sys.executable)
    while p and p != os.path.dirname(p):
        if p.endswith(".app"):
            return p
        p = os.path.dirname(p)
    return None


def can_self_update():
    """Only a packaged app replaces itself; from source we just point at the release page."""
    if not getattr(sys, "frozen", False):
        return False
    if platform_paths.IS_WIN:
        return True
    b = mac_bundle()
    return bool(platform_paths.IS_MAC and b and os.access(os.path.dirname(b), os.W_OK) and os.access(b, os.W_OK))


def work_dir():
    return tempfile.mkdtemp(prefix="ptz-tracker-update-")


def apply_windows(installer):
    """Runs the Inno Setup installer silently; it closes the app if needed and relaunches it afterwards."""
    flags = 0x00000008 | 0x00000200 if platform_paths.IS_WIN else 0  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([installer, *WIN_FLAGS], creationflags=flags, close_fds=True)


def apply_mac(zip_path, bundle=None, pid=None):
    """Unzips the new app and starts a helper that swaps it in once this process has quit. Raises on failure."""
    bundle = bundle or mac_bundle()
    if not bundle:
        raise RuntimeError("could not find the running PTZ Tracker.app")
    out = os.path.join(os.path.dirname(zip_path), "unpacked")
    os.makedirs(out, exist_ok=True)
    subprocess.run(["ditto", "-x", "-k", zip_path, out], check=True, capture_output=True)
    new = os.path.join(out, os.path.basename(bundle))
    if not os.path.isdir(new):
        apps = [n for n in os.listdir(out) if n.endswith(".app")]
        if not apps:
            raise RuntimeError("the download did not contain an app")
        new = os.path.join(out, apps[0])
    helper = os.path.join(os.path.dirname(zip_path), "swap.sh")
    with open(helper, "w") as f:
        f.write(MAC_HELPER)
    os.chmod(helper, 0o755)
    subprocess.Popen(
        ["/bin/sh", helper, str(pid or os.getpid()), new, bundle],
        start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
