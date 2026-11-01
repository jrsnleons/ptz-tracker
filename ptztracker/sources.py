"""Video inputs: NDI (cyndilib, bundles the NDI runtime), RTSP and local webcams."""
import json
import logging
import os
import threading
import time

import cv2
import numpy as np

from .platform_paths import IS_MAC, IS_WIN, config_dir, webcam_backends

log = logging.getLogger(__name__)

# low-latency RTSP: drop the ffmpeg buffer so the tracker sees the newest frame
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|max_delay;0"
)



def _ndi_extra_ips():
    """Points NDI discovery straight at the camera IP from the saved settings, as well as mDNS.

    Windows Firewall or a router that blocks multicast otherwise leaves the source list empty.
    NDI reads this from ndi-config.v1.json in NDI_CONFIG_DIR, so the user's own NDI config is copied
    and the IP added to it. Has to run before the NDI library loads.
    """
    if os.environ.get("NDI_CONFIG_DIR"):
        return
    try:
        with open(os.path.join(config_dir(), "config.json")) as f:
            ip = json.load(f).get("camera_ip", "").strip()
    except (OSError, ValueError):
        ip = ""
    if not ip:
        return
    if IS_WIN:
        own = os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "NDI", "ndi-config.v1.json")
    elif IS_MAC:
        own = os.path.expanduser("~/.ndi/ndi-config.v1.json")
    else:
        own = os.path.expanduser("~/.ndi/ndi-config.v1.json")
    try:
        with open(own) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    nets = cfg.setdefault("ndi", {}).setdefault("networks", {})
    ips = [x.strip() for x in str(nets.get("ips", "")).split(",") if x.strip()]
    if ip not in ips:
        ips.append(ip)
    nets["ips"] = ",".join(ips)
    folder = os.path.join(config_dir(), "ndi")
    try:
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "ndi-config.v1.json"), "w") as f:
            json.dump(cfg, f, indent=2)
        os.environ["NDI_CONFIG_DIR"] = folder
    except OSError:
        log.exception("could not write the NDI config")


_ndi_extra_ips()

try:
    from cyndilib.finder import Finder
    from cyndilib.receiver import Receiver
    from cyndilib.video_frame import VideoFrameSync
    from cyndilib.wrapper.ndi_recv import RecvBandwidth, RecvColorFormat

    HAVE_NDI = True
except Exception:  # pragma: no cover - NDI is optional
    log.exception("NDI library failed to load")
    HAVE_NDI = False


class NDIFinder:
    """Keeps a live list of NDI sources on the network."""

    def __init__(self):
        self.finder = None
        if HAVE_NDI:
            self.finder = Finder()
            self.finder.open()

    def names(self):
        if self.finder is None:
            return []
        self.finder.update_sources()
        return sorted(self.finder.get_source_names())

    def get(self, name, timeout=5.0):
        """Looks the source up, waiting a little since NDI discovery can lag."""
        if self.finder is None:
            return None
        deadline = time.monotonic() + timeout
        want = name.strip().lower()
        while True:
            self.finder.update_sources()
            names = self.finder.get_source_names()
            if name in names:
                return self.finder.get_source(name)
            # typed by hand: accept "LWNRA-PTZ_CAM" for "MACHINE (LWNRA-PTZ_CAM)" and different capitals
            close = [n for n in names if want and want in n.lower()]
            if len(close) == 1:
                return self.finder.get_source(close[0])
            if time.monotonic() > deadline:
                return None
            self.finder.wait_for_sources(0.5)

    def close(self):
        if self.finder is not None:
            self.finder.close()
            self.finder = None


class LatestFrame:
    """Hands the newest frame from a capture thread to the worker; older frames are dropped."""

    def __init__(self):
        self._cond = threading.Condition()
        self._frame = None
        self._new = False
        self.t = 0.0  # time.monotonic() the newest frame arrived

    def put(self, frame):
        with self._cond:
            self._frame, self._new, self.t = frame, True, time.monotonic()
            self._cond.notify()

    def get(self, timeout=0.05):
        """Waits up to `timeout` for a frame newer than the last one returned. Returns (frame, arrival time)."""
        with self._cond:
            if not self._new:
                self._cond.wait(timeout)
            if not self._new:
                return None, 0.0
            self._new = False
            return self._frame, self.t


class NDISource:
    """NDI receive on its own thread, so detection always works on the newest frame."""

    kind = "NDI"

    def __init__(self, finder: NDIFinder, name, high_bandwidth=False):
        if not HAVE_NDI:
            raise RuntimeError("cyndilib (NDI) is not installed")
        self.name = name
        self.receiver = Receiver(
            color_format=RecvColorFormat.BGRX_BGRA,
            bandwidth=RecvBandwidth.highest if high_bandwidth else RecvBandwidth.lowest,
            recv_name="PTZ Tracker",
        )
        self.vf = VideoFrameSync()
        self.receiver.frame_sync.set_video_frame(self.vf)
        src = finder.get(name)
        if src is None:
            raise RuntimeError(f"NDI source '{name}' not found")
        self.receiver.set_source(src)
        self._last_ts = None
        self.latest = LatestFrame()
        self._run = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _grab(self):
        if not self.receiver.is_connected():
            time.sleep(0.02)
            return None
        self.receiver.frame_sync.capture_video()
        xres, yres = self.vf.xres, self.vf.yres
        if xres == 0 or yres == 0:
            time.sleep(0.01)
            return None
        ts = self.vf.get_timestamp_posix()
        if ts == self._last_ts:
            time.sleep(0.002)  # no new frame yet
            return None
        self._last_ts = ts
        arr = np.asarray(self.vf.get_array())
        stride = self.vf.get_line_stride() or xres * 4
        img = arr[: stride * yres].reshape(yres, stride // 4, 4)[:, :xres, :3]
        return np.ascontiguousarray(img)

    def _loop(self):
        while self._run:
            try:
                frame = self._grab()
            except Exception:
                time.sleep(0.1)
                continue
            if frame is not None:
                self.latest.put(frame)

    def read(self, timeout=0.05):
        """Newest frame and when it arrived, or (None, 0) if nothing new came within `timeout`."""
        return self.latest.get(timeout)

    def close(self):
        self._run = False
        self._thread.join(timeout=1)
        try:
            self.receiver.disconnect()
        except Exception:
            pass


class CVSource:
    """RTSP URL or webcam index, read on its own thread so only the newest frame is kept."""

    kind = "CV"

    def __init__(self, target):
        self.name = str(target)
        if isinstance(target, int) or str(target).isdigit():
            self.cap = self._open_webcam(int(target))
            self.name = f"Webcam {target}"
        else:
            self.cap = cv2.VideoCapture(str(target), cv2.CAP_FFMPEG)
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if not self.cap.isOpened():
                raise RuntimeError(f"could not open video source: {target}")
        self.receiver = None
        self.latest = LatestFrame()
        self._run = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    @staticmethod
    def _open_webcam(index):
        """Tries each capture backend until one gives a real frame (DirectShow, then Media Foundation on Windows)."""
        for backend in webcam_backends():
            cap = cv2.VideoCapture(index, backend)
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                for _ in range(20):  # some cameras need a few reads before the first frame
                    ok, frame = cap.read()
                    if ok and frame is not None and frame.size:
                        return cap
                    time.sleep(0.05)
            cap.release()
            log.warning("webcam %d: no frames with backend %s", index, backend)
        raise RuntimeError(f"Webcam {index} didn't send any video. Check it isn't in use by another app, or try another number.")

    def _loop(self):
        while self._run:
            ok, frame = self.cap.read()
            if not ok:
                time.sleep(0.05)
                continue
            self.latest.put(frame)

    def read(self, timeout=0.05):
        return self.latest.get(timeout)

    def close(self):
        self._run = False
        self._thread.join(timeout=1)
        self.cap.release()
