"""Tracking engine: one thread per lightgun (camera -> vision -> serial)."""

import collections
import logging
import os
import threading
import time


from . import camera as cammod
from . import protocol
from . import vision
from .config import Config, PlayerConfig

log = logging.getLogger(__name__)


def _usb_parent(sysfs_device):
    """USB hub path that contains a device (the gun has an internal hub)."""
    try:
        real = os.path.realpath(sysfs_device)
    except OSError:
        return None
    parts = real.split("/")
    # .../usb1/1-1/1-1.2/1-1.2:1.0 -> parent hub "1-1"
    usb_parts = [p for p in parts if p and p[0].isdigit() and "-" in p and ":" not in p]
    if len(usb_parts) >= 2:
        return usb_parts[-2]
    return None


def camera_for_port(port, camera_name=""):
    """Find the video device that sits on the same USB hub as the gun's serial port."""
    tty = os.path.basename(port)
    parent = _usb_parent("/sys/class/tty/%s/device" % tty)
    if parent:
        for path, name in cammod.list_video_devices():
            node = os.path.basename(path)
            if _usb_parent("/sys/class/video4linux/%s/device" % node) == parent:
                return path
    return cammod.find_sinden_camera(camera_name)


def compute_camera_settings(camera_name, cfg: PlayerConfig, c: Config):
    """Camera controls as chosen by the official driver for this camera model."""
    name = camera_name or ""
    short = 0 < len(name) < 12
    exposure = cfg.get("CameraExposure").strip()
    exposure_auto = cfg.get("CameraExposureAuto").strip()
    if exposure == "" and name:
        if name[-1] in "nABCDEF" and len(name) > 11:
            exposure = "19"
        elif len(name) < 12:
            if name in ("SindenCamA", "SindenCamB"):
                exposure = "78"
            elif exposure_auto in ("", "3"):
                exposure = "A"
            else:
                exposure = "78"
        else:
            exposure = "78"
    elif exposure == "":
        exposure_auto = exposure_auto or "1"
        exposure = "79"

    settings = collections.OrderedDict()
    if exposure != "A":
        settings["exposure_auto"] = int(exposure_auto or "1")
        settings["exposure"] = int(exposure)
    else:
        settings["exposure_auto"] = int(exposure_auto or "3")

    brightness = int(cfg.get("CameraBrightness", "100") or 100)
    if len(name) < 12 and name in ("SindenCamA", "SindenCamB"):
        brightness -= 100
    settings["brightness"] = brightness
    settings["contrast"] = int(cfg.get("CameraContrast", "50") or 50)
    settings["saturation"] = c.get_int("CameraSaturation", 40)
    settings["hue"] = c.get_int("CameraHue", 0)
    settings["white_balance_auto"] = c.get_int("CameraWhiteBalanceAuto", 0)
    settings["white_balance"] = c.get_int("CameraWhiteBalance", 4500)
    if c.get("CameraZoom").strip():
        settings["zoom"] = c.get_int("CameraZoom", 0)
    if c.get("CameraGain").strip():
        settings["gain"] = c.get_int("CameraGain", 0)
    return settings, (not short)


class GunTracker:
    """Runs one lightgun. Thread-safe accessors are provided for the GUI."""

    def __init__(self, config: Config, player, port, joystick_mode=False,
                 on_event=None):
        self.config = config
        self.player = player
        self.pcfg = PlayerConfig(config, player)
        self.port = port
        self.joystick_mode = joystick_mode
        self.on_event = on_event or (lambda tracker, ev: None)
        self.name = "Lightgun%d" % player

        self.gun = None
        self.cam = None
        self.detector = None
        self.camera_path = None
        self.camera_upside_down = False
        self.calibration = (0.0, 0.0)
        self.in_calibration_mode = False
        self.status = "stopped"
        self.error = None
        self.process_ms = 0.0
        self.found_ratio = 0.0
        self.last_result = None
        self.last_sent = None
        self.buttons = (0, 0)

        self.preview_enabled = False
        self._preview = None
        self._lock = threading.Lock()
        self._cmds = collections.deque()
        self._stop = threading.Event()
        self._thread = None
        self._jitter_x = collections.deque(maxlen=5)
        self._jitter_y = collections.deque(maxlen=5)

    # -- public API (any thread) ----------------------------------------
    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    def stop(self, wait=True):
        self._stop.set()
        if wait and self._thread:
            self._thread.join(timeout=5)

    @property
    def alive(self):
        return self._thread is not None and self._thread.is_alive()

    def queue(self, fn):
        """Run fn(self) inside the tracker thread (serial / camera access)."""
        self._cmds.append(fn)

    def preview(self):
        with self._lock:
            return self._preview

    def info(self):
        g = self.gun
        return {
            "name": self.name,
            "port": self.port,
            "status": self.status,
            "error": self.error,
            "camera_name": g.camera_name if g else "",
            "camera_path": self.camera_path,
            "firmware": "v%d.%d" % g.firmware if g else "",
            "calibration": self.calibration,
            "fps": self.cam.fps if self.cam else 0.0,
            "process_ms": self.process_ms,
            "found_ratio": self.found_ratio,
            "in_calibration": self.in_calibration_mode,
            "buttons": self.buttons,
            "last_sent": self.last_sent,
            "upside_down": self.camera_upside_down,
        }

    # -- configuration --------------------------------------------------
    def _vision_settings(self):
        c = self.config
        return vision.VisionSettings(
            colour_rgb=c.get_rgb("ColourMatchRGB", (255, 255, 255)),
            colour_range=c.get_int("ColourMatchRange", 220),
            min_blob_size=20,
            close_mode=True,
            match_only_where_pointing=c.get_bool("MatchOnlyWherePointing"),
            gangsta_setting=self.pcfg.gangsta,
            gunsight_offset=4.9,
            camera_upside_down=self.camera_upside_down,
        )

    def reload_settings(self):
        """Re-read the config (vision + offsets). Call from any thread."""
        def apply(t):
            t.detector.s = t._vision_settings()
            t._load_offsets()
        self.queue(apply)

    def _load_offsets(self):
        c = self.config
        self.x_offset = c.get_float("OffsetX", 0.0)
        self.y_offset = c.get_float("OffsetY", 0.0)
        self.x_ratio = c.get_float("OffsetXRatio", 1.0) or 1.0
        self.y_ratio = c.get_float("OffsetYRatio", 1.0) or 1.0
        self.anti_jitter = c.get_bool("AntiJitter")
        self.anti_jitter_pct = c.get_float("AntiJitterPercent", 1.0)
        self.border_aspect = c.get("BorderAspectRatio", "16:9") or "16:9"

    def setup_gun(self):
        """Equivalent of SetupLightgunSettings() of the official driver."""
        g, p, c = self.gun, self.pcfg, self.config
        g.set_offscreen_reload(p.offscreen_reload)
        time.sleep(0.01)
        g.ser.drain_input()
        g.read_camera_name()
        log.info("%s StoredCameraName: %s", self.name, g.camera_name)

        actions, mods = p.button_actions(self.joystick_mode)
        g.assign_buttons(actions, mods)

        recoil = p.recoil()
        if recoil.agree_terms and recoil.enabled:
            g.assign_recoil(recoil)
            log.info("%s Assigned Recoil.", self.name)
        else:
            # Safer than the official driver: never arm the recoil unless the
            # user explicitly enabled it.
            g.switch_off_recoil()
            log.info("%s Recoil Off", self.name)

        g.set_calibration_enabled(c.get_bool("EnableCalibration", True))

        ox, oy = p.calibration_override()
        gx, gy = g.read_calibration()
        self.calibration = (ox if ox is not None else gx, oy if oy is not None else gy)
        log.info("%s Calibration: %s", self.name, self.calibration)

        g.read_firmware_version()
        log.info("%s Lightgun Firmware: v%d.%d", self.name, *g.firmware)
        g.set_secondary_serial(False)
        g.set_recoil_toggle(recoil.agree_terms and recoil.enabled)
        g.set_joystick_mode(self.joystick_mode)
        log.info("%s Joystick Mode %s", self.name, "Enabled" if self.joystick_mode else "Not Enabled")
        g.set_low_resource(False)
        g.ser.drain_input()

    def apply_camera_settings(self):
        settings, upside_down = compute_camera_settings(self.gun.camera_name, self.pcfg, self.config)
        self.camera_upside_down = upside_down
        ctl = self.cam.controls
        for k, v in settings.items():
            ctl.set(k, v)
        log.info("%s camera settings: %s (upside down: %s)", self.name, dict(settings), upside_down)

    # -- calibration -----------------------------------------------------
    def set_calibration(self, cal_x, cal_y, save=False):
        def apply(t):
            t.calibration = (round(cal_x, 2), round(cal_y, 2))
            if save:
                t.gun.save_calibration(*t.calibration)
            t.on_event(t, ("calibration_changed", t.calibration))
        self.queue(apply)

    def save_calibration_to_gun(self):
        self.queue(lambda t: (t.gun.save_calibration(*t.calibration),
                              t.on_event(t, ("calibration_saved", t.calibration))))

    def read_calibration_from_gun(self):
        def apply(t):
            t.calibration = t.gun.read_calibration()
            t.on_event(t, ("calibration_changed", t.calibration))
        self.queue(apply)

    def set_camera_control(self, name, value):
        self.queue(lambda t: t.cam.controls.set(name, value))

    def _perform_calibration(self):
        res = self.last_result
        cal = vision.calibration_from_detection(res.new_calibration if res else None,
                                                self.camera_upside_down)
        if cal is None:
            log.warning("%s calibration failed: border not fully visible", self.name)
            self.on_event(self, ("calibration_failed",))
            return
        self.calibration = cal
        log.info("%s new calibration %s", self.name, cal)
        if self.config.get_bool("AutoSaveCalibrationInLightgun", True):
            self.gun.save_calibration(*cal)
        self.on_event(self, ("calibration_changed", cal))

    # -- main loop -------------------------------------------------------
    def _run(self):
        try:
            self.status = "connecting"
            self.on_event(self, ("status", self.status))
            self.gun = protocol.Lightgun(self.port)
            self.gun.handshake()
            log.info("%s Starting.....", self.name)
            self.setup_gun()
            self.camera_path = self.pcfg.video_device or camera_for_port(self.port, self.gun.camera_name)
            if not self.camera_path:
                raise RuntimeError("Could not find Sinden Lightgun Camera Device.")
            w, h = (int(v) for v in (self.config.get("CameraRes", "640,480") or "640,480").split(","))
            self.cam = cammod.Camera(self.camera_path, w, h)
            self.cam.open()
            self.apply_camera_settings()
            self.detector = vision.BorderDetector(self._vision_settings())
            self._load_offsets()
            self.status = "running"
            self.on_event(self, ("status", self.status))
            self._loop()
        except Exception as e:  # noqa: BLE001
            log.exception("%s error", self.name)
            self.error = str(e)
            self.status = "error"
            self.on_event(self, ("status", self.status))
        finally:
            self._cleanup()

    def _cleanup(self):
        if self.cam:
            self.cam.close()
        if self.gun:
            try:
                self.gun.close()
            except OSError:
                pass
        if self.status != "error":
            self.status = "stopped"
        self.on_event(self, ("status", self.status))

    def _handle_events(self):
        for ev in self.gun.poll_events():
            kind = ev[0]
            if kind == "buttons":
                self.buttons = ev[1]
            elif kind == "enter_calibration":
                log.info("%s Entered Calibration Mode", self.name)
                self.in_calibration_mode = True
            elif kind == "calibration_set":
                log.info("%s Calibration Set", self.name)
                self._perform_calibration()
            elif kind == "exit_calibration":
                log.info("%s Exit Calibration", self.name)
                self.in_calibration_mode = False
            self.on_event(self, ev)

    def _loop(self):
        seq = None
        found_hist = collections.deque(maxlen=60)
        times = collections.deque(maxlen=100)
        while not self._stop.is_set():
            while self._cmds:
                fn = self._cmds.popleft()
                try:
                    fn(self)
                except Exception:  # noqa: BLE001
                    log.exception("command failed")
            if not self.cam.running:
                raise RuntimeError("Issue reading video frames, check USB connection")
            seq, frame = self.cam.get_frame(seq, timeout=0.5)
            if frame is None:
                continue
            t0 = time.perf_counter()
            res = self.detector.detect(frame, self.calibration)
            self.last_result = res
            found_hist.append(res.found)
            if res.found:
                x, y = vision.apply_user_offsets(res.x, res.y, res.gangsta, self.x_ratio,
                                                 self.x_offset, self.y_ratio, self.y_offset, 4.9)
                x = vision.border_to_screen(x, self.border_aspect)
                if self._should_move(x, y):
                    self.gun.send_position(x, y)
                    self.last_sent = (x, y)
            self._handle_events()
            times.append((time.perf_counter() - t0) * 1000.0)
            self.process_ms = sum(times) / len(times)
            self.found_ratio = sum(found_hist) / len(found_hist)
            if self.preview_enabled:
                with self._lock:
                    self._preview = (frame, res)

    def _should_move(self, x, y):
        if not self.anti_jitter:
            return True
        move = len(self._jitter_x) < 5 or any(
            abs(px - x) > self.anti_jitter_pct for px in self._jitter_x) or any(
            abs(py - y) > self.anti_jitter_pct for py in self._jitter_y)
        if move:
            self._jitter_x.append(x)
            self._jitter_y.append(y)
        return move


class Engine:
    """Manages all connected lightguns."""

    def __init__(self, config: Config, joystick_mode=None, on_event=None):
        self.config = config
        if joystick_mode is None:
            joystick_mode = config.get_bool("JoystickMode")
        self.joystick_mode = joystick_mode
        self.on_event = on_event
        self.trackers = []

    def start(self, ports=None):
        self.stop()
        ports = ports if ports is not None else protocol.find_lightgun_ports()
        if not ports:
            raise RuntimeError("Error please connect Sinden Lightgun.")
        for i, port in enumerate(ports[:2]):
            t = GunTracker(self.config, i + 1, port, self.joystick_mode, self.on_event)
            self.trackers.append(t)
            t.start()
        return self.trackers

    def stop(self):
        for t in self.trackers:
            t.stop(wait=False)
        for t in self.trackers:
            t.stop(wait=True)
        self.trackers = []

    @property
    def running(self):
        return any(t.alive for t in self.trackers)
