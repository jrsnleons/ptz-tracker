"""Real-camera test for the stage boundary (moves the camera, then puts it back).

Run on the church network:  .venv/bin/python -m tests.camera_stage_test [camera-ip]

1. old way: stop burst + immediate recall (the bug), with the camera's VISCA replies
2. new way: Homing from the same situation
3. outline accuracy: scene points from the wide shot, found again after pan + zoom
4. returns the camera to where it started
"""
import sys
import time

import cv2
import numpy as np

from ptztracker.ptz import VISCAoverIP
from ptztracker.sources import NDIFinder, NDISource
from ptztracker.stage import CanonPositionPoller, Homing, angle_to_pixel, pixel_to_angle

IP = sys.argv[1] if len(sys.argv) > 1 else "192.168.254.122"
NDI_NAME = "LWNRA-PTZ_CAM (Channel 1)"
PRESET = 10
WIDE = (5.0, -4.0, 45.0)
AWAY = (14.0, -2.0, 30.0)


def goto(cam, poller, pan, tilt, fov, timeout=25):
    """Closed-loop move using the position readback."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        p = poller.latest()
        if p is None:
            time.sleep(0.1)
            continue
        ep, et, ez = pan - p.pan, tilt - p.tilt, fov - p.hfov
        if abs(ep) < 0.15 and abs(et) < 0.15 and abs(ez) < 0.4:
            break
        sp = 0 if abs(ep) < 0.15 else np.sign(ep) * min(0.5, max(0.05, abs(ep) * 0.06))
        st = 0 if abs(et) < 0.15 else np.sign(et) * min(0.5, max(0.05, abs(et) * 0.06))
        cam.pan_tilt(float(sp), float(st))
        cam.zoom(0 if abs(ez) < 0.4 else (-1 if ez > 0 else 1) * (0.4 if abs(ez) > 3 else 0.15))
        time.sleep(0.08)
    cam.stop()
    time.sleep(1.5)
    return poller.latest()


def wait_settled(poller, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout and not poller.settled(0.8):
        time.sleep(0.1)
    return poller.latest()


def fmt(p):
    return f"pan {p.pan:+.2f}° tilt {p.tilt:+.2f}° view {p.hfov:.1f}°"


def replies_since(cam, t):
    cam.poll_replies()
    return [f"seq {r[1]} {r[2]} {r[3]}".strip() for r in cam.replies if r[0] >= t]


def grab(src, seconds=1.0):
    t0, fr = time.time(), None
    while time.time() - t0 < seconds:
        x = src.read()
        if x is not None:
            fr = x
    return fr


def outline_check(src, cam, poller):
    """Pick distinctive patches on the wide shot, predict where they land after pan+zoom, find them."""
    pa = goto(cam, poller, *WIDE)
    fa = grab(src, 1.5)
    h, w = fa.shape[:2]
    gray = cv2.cvtColor(fa, cv2.COLOR_BGR2GRAY)
    corners = cv2.goodFeaturesToTrack(gray, 40, 0.05, 30)
    pb = goto(cam, poller, *AWAY)
    fb = grab(src, 1.5)
    errs = []
    for (x, y) in corners.reshape(-1, 2):
        pred = angle_to_pixel(*pixel_to_angle(x, y, pa, w, h), pb, w, h)
        if pred is None or not (30 < pred[0] < w - 30 and 30 < pred[1] < h - 30) or not (16 < x < w - 16 and 16 < y < h - 16):
            continue
        # the patch is magnified by the zoom change; scale it before matching
        scale = np.tan(np.radians(pa.hfov) / 2) / np.tan(np.radians(pb.hfov) / 2)
        patch = cv2.resize(fa[int(y) - 16 : int(y) + 16, int(x) - 16 : int(x) + 16], None, fx=scale, fy=scale)
        res = cv2.matchTemplate(fb, patch, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(res)
        if score < 0.7:
            continue
        found = (loc[0] + patch.shape[1] / 2, loc[1] + patch.shape[0] / 2)
        errs.append(np.hypot(found[0] - pred[0], found[1] - pred[1]))
    return pa, pb, errs


def main():
    poller = CanonPositionPoller(IP)
    cam = VISCAoverIP(IP)
    time.sleep(1)
    start = poller.latest()
    if start is None:
        print(f"camera position not readable at {IP}: {poller.error}")
        return 1
    print("start:", fmt(start), "| VISCA replies:", "on" if cam.listening else "OFF (port 52381 busy)")
    f = NDIFinder()
    src = NDISource(f, NDI_NAME)
    try:
        home = goto(cam, poller, *WIDE)
        cam.store_preset(PRESET)
        time.sleep(0.5)
        home_d = {"pan": home.pan, "tilt": home.tilt, "hfov": home.hfov}
        print("stage shot stored as preset", PRESET, "at", fmt(home))

        print("\n1) old way: stop burst then recall in the same millisecond")
        goto(cam, poller, *AWAY)
        t = time.monotonic()
        cam.stop()
        cam.recall_preset(PRESET)
        p = wait_settled(poller)
        print("   ended at", fmt(p), "->", "REACHED HOME" if abs(p.pan - home.pan) < 0.4 and abs(p.hfov - home.hfov) < 1 else "DID NOT REACH HOME")
        print("   camera replies:", replies_since(cam, t) or "none")

        for run in (1, 2, 3):
            print(f"\n2.{run}) new way: Homing from the same situation")
            goto(cam, poller, *AWAY)
            t = time.monotonic()
            cam.stop()
            hm = Homing(cam, poller, PRESET, home_d)
            while not hm.step():
                time.sleep(1 / 30)
            p = wait_settled(poller)
            print(f"   result {hm.result!r} in {time.monotonic() - t:.1f}s, ended at", fmt(p))
            print("   camera replies:", replies_since(cam, t) or "none")

        print("\n3) outline accuracy after pan + zoom")
        pa, pb, errs = outline_check(src, cam, poller)
        if errs:
            print(f"   {len(errs)} scene points: median error {np.median(errs):.1f} px, worst {max(errs):.1f} px (frame 640 px wide)")
        else:
            print("   not enough matchable points in view")
    finally:
        p = goto(cam, poller, start.pan, start.tilt, start.hfov)
        print("\nrestored:", fmt(p), "(started at", fmt(start) + ")")
        cam.close()
        poller.close()
        src.close()
        f.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
