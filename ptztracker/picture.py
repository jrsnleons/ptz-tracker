"""Picture settings for NDI PTZ and VISCA, which can't read the camera back: the values we last sent (per camera),
looks, and sending groups. Over the Canon API the camera's own values are used instead (picture_panel.py)."""
import logging

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
