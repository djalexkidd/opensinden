"""Sinden camera access: V4L2 capture through OpenCV + V4L2 controls via ioctl."""

import fcntl
import glob
import logging
import os
import re
import struct
import threading
import time

import cv2

log = logging.getLogger(__name__)


def _iowr(nr, size):
    return (3 << 30) | (size << 16) | (ord("V") << 8) | nr


_QUERYCTRL_FMT = "II32siiiiI2I"
VIDIOC_QUERYCTRL = _iowr(36, struct.calcsize(_QUERYCTRL_FMT))
VIDIOC_G_CTRL = _iowr(27, 8)
VIDIOC_S_CTRL = _iowr(28, 8)

_CID_BASE = 0x00980900
_CID_CAMERA_BASE = 0x009A0900
CONTROLS = {
    "brightness": _CID_BASE + 0,
    "contrast": _CID_BASE + 1,
    "saturation": _CID_BASE + 2,
    "hue": _CID_BASE + 3,
    "white_balance_auto": _CID_BASE + 12,
    "gain": _CID_BASE + 19,
    "white_balance": _CID_BASE + 26,
    "exposure_auto": _CID_CAMERA_BASE + 1,
    "exposure": _CID_CAMERA_BASE + 2,
    "zoom": _CID_CAMERA_BASE + 13,
}


def list_video_devices():
    """Return [(path, name)] for capture nodes (index 0) of all V4L2 devices."""
    out = []
    for node in sorted(glob.glob("/sys/class/video4linux/video*"),
                       key=lambda p: int(re.sub(r"\D", "", os.path.basename(p)) or 0)):
        try:
            with open(os.path.join(node, "name")) as f:
                name = f.read().strip()
            with open(os.path.join(node, "index")) as f:
                index = f.read().strip()
        except OSError:
            continue
        if index != "0":
            continue
        out.append(("/dev/" + os.path.basename(node), name))
    return out


def find_sinden_camera(camera_name=""):
    """Find the video device of the lightgun camera (name reported by the gun)."""
    devices = list_video_devices()
    if camera_name:
        for path, name in devices:
            if camera_name in name:
                return path
    for path, name in devices:
        if "Sinden" in name:
            return path
    return None


class CameraControls:
    """Get / set V4L2 controls on a device (can be used while streaming)."""

    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def query(self, name):
        cid = CONTROLS[name]
        buf = bytearray(struct.pack(_QUERYCTRL_FMT, cid, 0, b"", 0, 0, 0, 0, 0, 0, 0))
        try:
            fcntl.ioctl(self.fd, VIDIOC_QUERYCTRL, buf)
        except OSError:
            return None
        _id, _type, cname, mn, mx, step, default, flags, _r1, _r2 = struct.unpack(_QUERYCTRL_FMT, buf)
        if flags & 0x0001:  # V4L2_CTRL_FLAG_DISABLED
            return None
        return {"name": cname.rstrip(b"\0").decode(), "min": mn, "max": mx,
                "step": step, "default": default, "flags": flags}

    def get(self, name):
        buf = bytearray(struct.pack("Ii", CONTROLS[name], 0))
        try:
            fcntl.ioctl(self.fd, VIDIOC_G_CTRL, buf)
        except OSError:
            return None
        return struct.unpack("Ii", buf)[1]

    def set(self, name, value):
        info = self.query(name)
        if info is None:
            log.debug("control %s not supported", name)
            return False
        value = int(value)
        value = max(info["min"], min(info["max"], value))
        buf = bytearray(struct.pack("Ii", CONTROLS[name], value))
        try:
            fcntl.ioctl(self.fd, VIDIOC_S_CTRL, buf)
        except OSError as e:
            log.debug("could not set %s=%s: %s", name, value, e)
            return False
        return True

    def all(self):
        res = {}
        for name in CONTROLS:
            info = self.query(name)
            if info is not None:
                info["value"] = self.get(name)
                res[name] = info
        return res


class Camera:
    """Threaded capture: always keeps the most recent frame."""

    def __init__(self, path, width=640, height=480):
        self.path = path
        self.width = width
        self.height = height
        self.cap = None
        self.controls = None
        self._frame = None
        self._seq = 0
        self._cond = threading.Condition()
        self._running = False
        self._thread = None
        self.fps = 0.0

    def open(self):
        cap = cv2.VideoCapture(self.path, cv2.CAP_V4L2)
        if not cap.isOpened():
            raise RuntimeError("cannot open camera %s" % self.path)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, 60)
        self.width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.cap = cap
        self.controls = CameraControls(self.path)
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="camera", daemon=True)
        self._thread.start()

    def _loop(self):
        failures = 0
        t0 = time.monotonic()
        count = 0
        while self._running:
            ok, frame = self.cap.read()
            if not ok:
                failures += 1
                if failures > 100:
                    log.error("Issue reading video frames, check USB connection")
                    self._running = False
                    with self._cond:
                        self._cond.notify_all()
                    break
                time.sleep(0.01)
                continue
            failures = 0
            count += 1
            now = time.monotonic()
            if now - t0 >= 1.0:
                self.fps = count / (now - t0)
                count = 0
                t0 = now
            with self._cond:
                self._frame = frame
                self._seq += 1
                self._cond.notify_all()

    @property
    def running(self):
        return self._running

    def get_frame(self, last_seq=None, timeout=1.0):
        """Return (seq, frame) of a frame newer than last_seq."""
        with self._cond:
            if last_seq is not None:
                self._cond.wait_for(lambda: self._seq != last_seq or not self._running, timeout)
            return self._seq, self._frame

    def close(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        if self.cap:
            self.cap.release()
            self.cap = None
        if self.controls:
            self.controls.close()
            self.controls = None
