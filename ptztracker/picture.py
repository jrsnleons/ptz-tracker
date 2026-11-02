"""Camera picture settings: the values we last sent (per camera), looks, sending groups, and the Canon HTTP probe."""
import logging
import threading
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

DEFAULTS = {
    "exposure": "auto", "brightness": 0.5, "iris": 0.5, "gain": 0.0, "shutter": 0.5,
    "wb": "auto", "red": 0.5, "blue": 0.5, "contrast": 0.5, "sharpness": 0.5, "saturation": 0.5,
}  # fmt: skip
# values that travel together in one camera command: name -> (needs capability, fields)
GROUPS = {
    "exposure_level": ("exposure", ("brightness",)),
    "exposure_fine": ("exposure", ("iris", "gain", "shutter")),
    "wb_rgb": ("wb", ("red", "blue")),
    "aperture": ("aperture", ("sharpness",)),
}
FIELD_GROUP = {f: g for g, (_, fields) in GROUPS.items() for f in fields}
HTTP_WORDS = ("contrast", "sharpness", "saturation")


def clean(values):
    """Saved picture values merged over the defaults, with anything unusable dropped."""
    out = dict(DEFAULTS)
    for k, v in (values or {}).items():
        if k not in DEFAULTS:
            continue
        if k in ("exposure", "wb"):
            if isinstance(v, str):
                out[k] = v
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = min(1.0, max(0.0, float(v)))
    if out["exposure"] not in ("auto", "manual"):
        out["exposure"] = "auto"
    if out["wb"] not in ("auto", "indoor", "outdoor", "once", "manual"):
        out["wb"] = "auto"
    return out


def send_group(cam, group, v):
    if group == "exposure_level":
        cam.exposure_level(v["brightness"])
    elif group == "exposure_fine":
        cam.exposure_fine(v["iris"], v["gain"], v["shutter"])
    elif group == "wb_rgb":
        cam.white_balance_rgb(v["red"], v["blue"])
    elif group == "aperture":
        cam.aperture(v["sharpness"])


def send_all(cam, v):
    """Sends a whole picture (a look): modes first, then the values that apply to them."""
    cam.exposure_mode(v["exposure"])
    if v["exposure"] == "manual":
        send_group(cam, "exposure_fine", v)
    else:
        send_group(cam, "exposure_level", v)
    cam.white_balance_mode(v["wb"])
    if v["wb"] == "manual":
        send_group(cam, "wb_rgb", v)
    if "aperture" in cam.caps:
        send_group(cam, "aperture", v)


def reset_to_auto(v):
    v = dict(v)
    v["exposure"] = v["wb"] = "auto"
    return v


# display only: the camera's real stops differ a little by model
def brightness_text(x):
    return f"{round(x * 14 - 7):+d}"


def iris_text(x):
    return f"F{11 * (1.8 / 11) ** x:.1f}"


def gain_text(x):
    return f"{x * 30:.0f} dB"


def shutter_text(x):
    return f"1/{round(2 * 4000 ** x)}"


def percent_text(x):
    return f"{round(x * 100)}%"


class CanonPicture:
    """Reads the camera's whole info.cgi list once on connect (written to the log), and finds Contrast / Sharpness /
    Saturation items if the camera has them. Untested against the real CR-N300: every step is defensive and runs
    on a background thread, results come back through `on_ready`."""

    BASE = "-wvhttp-01-"

    def __init__(self, host, on_ready=None):
        self.host, self.on_ready = host, on_ready
        self.items = {}  # word -> key name on the camera
        self.values = {}  # key name -> current text value
        self.all = {}
        threading.Thread(target=self._probe, daemon=True).start()

    def _get(self, path, timeout=3.0):
        return urllib.request.urlopen(f"http://{self.host}/{self.BASE}/{path}", timeout=timeout).read().decode(errors="replace")

    @staticmethod
    def parse(text):
        return dict(line.split(":=", 1) for line in text.splitlines() if ":=" in line)

    @staticmethod
    def find_items(keys):
        """word -> first key containing it (case-insensitive); skips range/limit helper keys."""
        found = {}
        for word in HTTP_WORDS:
            for k in keys:
                low = k.lower()
                if word in low and not low.endswith((".min", ".max", ".range", ".list", ".step")) and word not in found:
                    found[word] = k
        return found

    def _probe(self):
        try:
            self.all = self.parse(self._get("info.cgi"))
            log.info("Canon info.cgi: %d items\n%s", len(self.all), "\n".join(f"  {k} = {v}" for k, v in sorted(self.all.items())))
            self.items = self.find_items(self.all)
            self.values = {k: self.all[k] for k in self.items.values()}
            log.info("Canon picture items found: %s", self.items or "none")
        except Exception as e:
            log.info("Canon info.cgi probe failed: %s", e)
        if self.on_ready:
            try:
                self.on_ready(dict(self.items))
            except Exception:
                log.exception("picture probe callback failed")

    def value_for(self, word, x):
        """Slider position 0..1 -> the camera's number. The range comes from <key>.min / <key>.max when the list has
        them; otherwise -10..+10 is a guess, and the log shows what the camera really uses."""
        key = self.items.get(word)
        try:
            lo, hi = int(self.all[key + ".min"]), int(self.all[key + ".max"])
        except (KeyError, TypeError, ValueError):
            lo, hi = -10, 10
        return round(lo + min(1.0, max(0.0, x)) * (hi - lo))

    def set(self, word, x):
        """Sets one item from a slider position (0..1) without blocking the caller."""
        key = self.items.get(word)
        if key is None:
            return
        value = self.value_for(word, x)

        def go():
            try:
                self._get(f"control.cgi?{urllib.parse.quote(key)}={urllib.parse.quote(str(value))}", timeout=2.0)
            except Exception as e:
                log.info("Canon set %s=%s failed: %s", key, value, e)

        threading.Thread(target=go, daemon=True).start()
