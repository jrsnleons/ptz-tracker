"""Read-only check of the real camera's web API, to confirm what the Canon API backend relies on. Moves nothing.

    .venv-win\\Scripts\\python -m tests.canon_probe 192.168.254.122 [user password] > canon-probe.txt

Prints the whole info list (presets, picture keys and ranges, speed limits), then whether a control session opens
(with the login if given) and closes again. Send canon-probe.txt back so the keys and scales can be confirmed.
"""
import sys
import urllib.error
import urllib.parse
import urllib.request

from ptztracker.canon_http import CanonClient, parse

WATCH = ("c.1.pan.speed", "c.1.tilt.speed", "c.1.zoom.speed", "p.ptzspeed", "p.ptztime", "p.1.", "c.1.me.", "c.1.wb", "c.1.ac", "c.1.exp")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    host, user, pw = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else ""), (sys.argv[3] if len(sys.argv) > 3 else "")
    handlers = []
    if user:
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, f"http://{host}/", user, pw)
        handlers = [urllib.request.HTTPDigestAuthHandler(mgr), urllib.request.HTTPBasicAuthHandler(mgr)]
    opener = urllib.request.build_opener(*handlers)

    def get(path, **params):
        url = f"http://{host}/{CanonClient.BASE}/{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        with opener.open(url, timeout=5) as r:
            return r.read().decode(errors="replace")

    info = None
    for name in ("info", "info.cgi"):
        try:
            info = parse(get(name))
            print(f"== {name}: OK, {len(info)} items")
            break
        except urllib.error.HTTPError as e:
            print(f"== {name}: HTTP {e.code}")
    if info is None:
        sys.exit("no info list")
    print("\n== keys the app uses")
    for k in sorted(info):
        if k.startswith(WATCH):
            print(f"  {k} = {info[k]}")
    print("\n== every key")
    for k in sorted(info):
        print(f"  {k} = {info[k]}")
    print("\n== control session")
    try:
        d = parse(get("open", **{"s.priority": 40}))
        print(f"  open: {d}")
        if d.get("s"):
            print("  close:", get("close", s=d["s"]).strip() or "OK")
    except urllib.error.HTTPError as e:
        print(f"  open: HTTP {e.code} {e.headers.get('WWW-Authenticate', '')}")
    except Exception as e:
        print(f"  open failed: {e}")


if __name__ == "__main__":
    main()
