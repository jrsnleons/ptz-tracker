"""Video inputs: NDI (cyndilib, bundles the NDI runtime), RTSP and local webcams."""
import os
import threading
import time

import cv2
import numpy as np

# low-latency RTSP: drop the ffmpeg buffer so the tracker sees the newest frame
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|max_delay;0"
)

try:
    from cyndilib.finder import Finder
    from cyndilib.receiver import Receiver
    from cyndilib.video_frame import VideoFrameSync
    from cyndilib.wrapper.ndi_recv import RecvBandwidth, RecvColorFormat

    HAVE_NDI = True
except Exception:  # pragma: no cover - NDI is optional
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
        while True:
            self.finder.update_sources()
            if name in self.finder.get_source_names():
                return self.finder.get_source(name)
            if time.monotonic() > deadline:
                return None
            self.finder.wait_for_sources(0.5)

    def close(self):
        if self.finder is not None:
            self.finder.close()
            self.finder = None


class NDISource:
    kind = "NDI"

    def __init__(self, finder: NDIFinder, name, high_bandwidth=False):
        if not HAVE_NDI:
            raise RuntimeError("cyndilib (NDI) is not installed")
        self.name = name
        self.receiver = Receiver(
            color_format=RecvColorFormat.BGRX_BGRA,
            bandwidth=RecvBandwidth.highest if high_bandwidth else RecvBandwidth.lowest,
            recv_name="PTZ Tracker Mac",
        )
        self.vf = VideoFrameSync()
        self.receiver.frame_sync.set_video_frame(self.vf)
        src = finder.get(name)
        if src is None:
            raise RuntimeError(f"NDI source '{name}' not found")
        self.receiver.set_source(src)
        self._last_ts = None

    def read(self):
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
            time.sleep(0.004)  # no new frame yet
            return None
        self._last_ts = ts
        arr = np.asarray(self.vf.get_array())
        stride = self.vf.get_line_stride() or xres * 4
        img = arr[: stride * yres].reshape(yres, stride // 4, 4)[:, :xres, :3]
        return np.ascontiguousarray(img)

    def close(self):
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
            self.cap = cv2.VideoCapture(int(target), cv2.CAP_AVFOUNDATION)
        else:
            self.cap = cv2.VideoCapture(str(target), cv2.CAP_FFMPEG)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not self.cap.isOpened():
            raise RuntimeError(f"could not open video source: {target}")
        self.receiver = None
        self._frame = None
        self._new = False
        self._lock = threading.Lock()
        self._run = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while self._run:
            ok, frame = self.cap.read()
            if not ok:
                time.sleep(0.05)
                continue
            with self._lock:
                self._frame = frame
                self._new = True

    def read(self):
        with self._lock:
            if not self._new:
                frame = None
            else:
                frame, self._new = self._frame, False
        if frame is None:
            time.sleep(0.004)
        return frame

    def close(self):
        self._run = False
        self._thread.join(timeout=1)
        self.cap.release()
