"""Serial protocol spoken by the Sinden Lightgun firmware.

Every command is a 7 byte frame: 0xAA <cmd> <a> <b> <c> <d> 0xBB.
The gun itself is the USB HID mouse / keyboard / joystick: the PC only sends
the aim position (as a percentage of the screen) and the configuration.
"""

import glob
import hashlib
import logging
import os
import re
import time
import uuid

from .serialport import SerialPort

log = logging.getLogger(__name__)

SINDEN_USB_IDS = ("16c0/f01", "16c0/f02", "16c0/f38", "16c0/f39")

# Commands (byte 1 of the frame)
CMD_SET_POSITION = 0x28
CMD_SET_POSITION_LOWRES_TRIGGER = 0x29
CMD_SECONDARY_SERIAL_ON = 0x32
CMD_SECONDARY_SERIAL_OFF = 0x33
CMD_OFFSCREEN_RELOAD_ON = 0x36
CMD_OFFSCREEN_RELOAD_OFF = 0x37
CMD_BUTTON_ASSIGN = 0x3C
CMD_FIRMWARE_VERSION = 0x65   # 'e'
CMD_CAMERA_NAME = 0x66        # 'f'
CMD_READ_CALIBRATION_X = 0x68  # 'h'
CMD_READ_CALIBRATION_Y = 0x69  # 'i'
CMD_SAVE_CALIBRATION_X = 0x6A  # 'j'
CMD_SAVE_CALIBRATION_Y = 0x6B  # 'k'
CMD_HANDSHAKE_2 = 0x6D        # 'm'
CMD_HANDSHAKE_1 = 0x6E        # 'n'
CMD_START = 0x79              # 'y'
CMD_RECOIL_ENABLE = 0xA1
CMD_RECOIL_AUTO_PARAMS = 0xA2
CMD_RECOIL_REPEAT = 0xA3
CMD_RECOIL_EVENTS = 0xA4
CMD_RECOIL_BUTTONS = 0xA5
CMD_RECOIL_STRENGTH = 0xA7
CMD_RECOIL_AB = 0xAB
CMD_RECOIL_VOLTAGE = 0xAC
CMD_CALIBRATION_ENABLE = 0xB4
CMD_RECOIL_TOGGLE = 0xB5
CMD_JOYSTICK_MODE = 0xB6
CMD_LOW_RESOURCE = 0xB7

# Bytes sent by the gun
EVT_TRIGGER_ENABLED = 0x78    # low resource mode only
EVT_TRIGGER_DISABLED = 0x79
EVT_ENTER_CALIBRATION = 0xC8
EVT_CALIBRATION_SET = 0xC9
EVT_EXIT_CALIBRATION = 0xCA
EVT_BUTTON_STATE = 0xFE

_SECRET_1 = ("ThisSpaceIsBlankThisSpaceIsBlankSindenLightgun3642"
             "94735243894HaveANiceDay")
_SECRET_2 = ("12345678901234567890123456789012603416630855321700"
             "74617363215964123456")

# Index of the button slots in the firmware (byte 3 of CMD_BUTTON_ASSIGN)
BUTTON_SLOTS = [
    ("Trigger", 100), ("FrontLeft", 101), ("RearLeft", 102),
    ("FrontRight", 103), ("RearRight", 104), ("Up", 105), ("Down", 106),
    ("Left", 107), ("Right", 108), ("PumpAction", 109),
    ("TriggerOffscreen", 110), ("FrontLeftOffscreen", 111),
    ("RearLeftOffscreen", 112), ("FrontRightOffscreen", 113),
    ("RearRightOffscreen", 114), ("UpOffscreen", 115),
    ("DownOffscreen", 116), ("LeftOffscreen", 117),
    ("RightOffscreen", 118), ("PumpActionOffscreen", 119),
]
MODIFIER_SLOTS = [
    ("Trigger", 120), ("FrontLeft", 121), ("RearLeft", 122),
    ("FrontRight", 123), ("RearRight", 124), ("Up", 125), ("Down", 126),
    ("Left", 127), ("Right", 128), ("PumpAction", 129),
    ("TriggerOffscreen", 130), ("FrontLeftOffscreen", 131),
    ("RearLeftOffscreen", 132), ("FrontRightOffscreen", 133),
    ("RearRightOffscreen", 134), ("UpOffscreen", 135),
    ("DownOffscreen", 136), ("LeftOffscreen", 137),
    ("RightOffscreen", 138), ("PumpActionOffscreen", 139),
]


def _build_actions():
    """List of assignable actions: index (as stored in the config) -> (label, firmware code)."""
    actions = [
        ("None", 0), ("MouseLeft", 0xFF), ("MouseMiddle", 0xFE),
        ("MouseRight", 0xFD), ("Pause Movement", 0xFC),
        ("Turbo Fire MouseLeft", 0xFB),
        ("Turbo Fire/Reload MouseLeft/Right", 0xFA),
        ("Border On/Off (Alt B)", 0xF9),
    ]
    actions += [(str(d), ord(str(d))) for d in range(10)]
    actions += [(chr(c), c) for c in range(ord("A"), ord("Z") + 1)]
    actions += [(chr(c), c) for c in range(ord("a"), ord("z") + 1)]
    actions += [("Return", 0xB0), ("Space", 0x20), ("Escape", 0xB1), ("Tab", 0xB3)]
    actions += [("Up", 0xDA), ("Down", 0xD9), ("Left", 0xD8), ("Right", 0xD7)]
    actions += [("+", 43), (",", 44), ("-", 45), (".", 46)]
    actions += [("F%d" % i, 0xC1 + i) for i in range(1, 13)]
    actions += [("Joystick Button %d" % i, i) for i in range(1, 21)]
    return actions


ACTIONS = _build_actions()

# Modifier keys (Arduino keyboard codes). The config file stores the raw value.
MODIFIERS = [("None", 0), ("Ctrl", 0x80), ("Shift", 0x81), ("Alt", 0x82),
             ("Super", 0x83), ("Right Ctrl", 0x84), ("Right Shift", 0x85),
             ("AltGr", 0x86)]


class HandshakeError(Exception):
    pass


def find_lightgun_ports():
    """Return the list of /dev/ttyACMn that belong to a Sinden Lightgun."""
    ports = []
    for path in sorted(glob.glob("/sys/class/tty/ttyACM*"),
                       key=lambda p: int(re.sub(r"\D", "", os.path.basename(p)) or 0)):
        try:
            with open(os.path.join(path, "device", "uevent")) as f:
                uevent = f.read()
        except OSError:
            continue
        m = re.search(r"^PRODUCT=(\S+)", uevent, re.M)
        if not m:
            continue
        product = m.group(1).lower()
        if any(product.startswith(pid) for pid in SINDEN_USB_IDS):
            ports.append("/dev/" + os.path.basename(path))
    return ports


def frame(cmd, a=0, b=0, c=0, d=0):
    return bytes([0xAA, cmd & 0xFF, a & 0xFF, b & 0xFF, c & 0xFF, d & 0xFF, 0xBB])


class Lightgun:
    """Connection to one Sinden Lightgun over its CDC-ACM serial port."""

    def __init__(self, port):
        self.port_path = port
        self.ser = SerialPort(port, 115200)
        self.camera_name = ""
        self.firmware = (0, 0)
        self.calibration = (0.0, 0.0)
        self.button_state = (0, 0)

    def close(self):
        self.ser.close()

    # -- low level -------------------------------------------------------
    def send(self, cmd, a=0, b=0, c=0, d=0):
        self.ser.write(frame(cmd, a, b, c, d))

    def _drain(self):
        time.sleep(0.01)
        self.ser.drain_input()

    # -- handshake -------------------------------------------------------
    def handshake(self):
        ser = self.ser
        self.send(CMD_HANDSHAKE_1)
        if ser.in_waiting() > 0:
            time.sleep(0.1)
            ser.drain_input()

        # Step 1: we send a random hash, the gun proves it knows the shared salt.
        challenge = hashlib.sha256(str(uuid.uuid4()).encode("ascii")).digest()
        ser.write(challenge)
        expected = hashlib.sha256(challenge + _SECRET_1[32:73].encode("ascii")).digest()
        if not ser.wait_for(32, timeout=3.0):
            raise HandshakeError("no answer from the lightgun (step 1)")
        time.sleep(0.005)
        answer = ser.read(32)
        gun_ok = answer == expected
        ser.drain_input()

        # Step 2: the gun sends a challenge, we answer.
        self.send(CMD_HANDSHAKE_2)
        if not ser.wait_for(32, timeout=3.0):
            raise HandshakeError("no answer from the lightgun (step 2)")
        gun_challenge = ser.read(32)
        reply = hashlib.sha256(gun_challenge + _SECRET_2[32:64].encode("ascii")).digest()
        ser.write(reply)
        if not ser.wait_for(5, timeout=3.0):
            raise HandshakeError("no answer from the lightgun (step 3)")
        line = ser.readline()
        if line != "true" or not gun_ok:
            raise HandshakeError("lightgun refused the handshake (%r, gun_ok=%s)" % (line, gun_ok))
        self.send(CMD_START)
        time.sleep(0.15)
        self.send(CMD_START)

    # -- queries ---------------------------------------------------------
    def read_camera_name(self):
        self._drain()
        self.send(CMD_CAMERA_NAME)
        time.sleep(0.05)
        data = self.ser.read(15, timeout=0.5)
        name = data.decode("latin-1").rstrip(" \t\r\n")
        self.camera_name = name
        return name

    def _read_calibration(self, cmd):
        time.sleep(0.1)
        self.ser.drain_input()
        self.send(cmd)
        hi = self.ser.read(1, timeout=0.3)
        time.sleep(0.05)
        lo = self.ser.read(1, timeout=0.3)
        hi = hi[0] if hi else 0
        lo = lo[0] if lo else 0
        value = hi * 256 + lo - 10000.0
        if value != 0:
            value /= 100.0
        if value > 99 or value < -99:
            value = 0.0
        return value

    def read_calibration(self):
        x = self._read_calibration(CMD_READ_CALIBRATION_X)
        y = self._read_calibration(CMD_READ_CALIBRATION_Y)
        self.calibration = (x, y)
        return x, y

    def read_firmware_version(self):
        self.ser.drain_input()
        self.send(CMD_FIRMWARE_VERSION)
        time.sleep(0.05)
        data = self.ser.read(2, timeout=0.5)
        if len(data) == 2:
            self.firmware = (data[0], data[1])
        return self.firmware

    # -- settings --------------------------------------------------------
    def set_offscreen_reload(self, enabled):
        self.send(CMD_OFFSCREEN_RELOAD_ON if enabled else CMD_OFFSCREEN_RELOAD_OFF)

    def set_calibration_enabled(self, enabled):
        self.send(CMD_CALIBRATION_ENABLE, 1 if enabled else 0)

    def set_secondary_serial(self, enabled):
        self.send(CMD_SECONDARY_SERIAL_ON if enabled else CMD_SECONDARY_SERIAL_OFF)
        time.sleep(0.005)

    def set_recoil_toggle(self, enabled):
        self.send(CMD_RECOIL_TOGGLE, 1 if enabled else 0)
        time.sleep(0.005)

    def set_joystick_mode(self, enabled):
        self.send(CMD_JOYSTICK_MODE, 1 if enabled else 0)
        time.sleep(0.005)

    def set_low_resource(self, enabled):
        self.send(CMD_LOW_RESOURCE, 1 if enabled else 0)
        time.sleep(0.005)

    def assign_button(self, slot, firmware_code):
        self.send(CMD_BUTTON_ASSIGN, 0, slot, 0, firmware_code)

    def assign_buttons(self, actions, modifiers):
        """actions / modifiers: dict slot name -> action index / modifier raw value."""
        for name, slot in BUTTON_SLOTS:
            idx = int(actions.get(name, 0))
            code = ACTIONS[idx][1] if 0 <= idx < len(ACTIONS) else 0
            self.assign_button(slot, code)
        for name, slot in MODIFIER_SLOTS:
            self.assign_button(slot, int(modifiers.get(name, 0)) & 0xFF)

    def switch_off_recoil(self):
        self.send(CMD_RECOIL_ENABLE, 0)
        time.sleep(0.1)

    def assign_recoil(self, r):
        """Send the recoil settings. `r` is a RecoilSettings-like object.

        Mirrors the official driver. Only used when the user enabled recoil.
        """
        buf = bytearray(frame(CMD_RECOIL_AUTO_PARAMS, r.auto_strength, r.auto_start_delay,
                              r.auto_strength, r.auto_delay_between_pulses))
        self.ser.write(buf)
        time.sleep(0.1)
        self.ser.write(frame(CMD_RECOIL_ENABLE, 1 if r.agree_terms else 0))
        time.sleep(0.1)
        self.ser.write(frame(CMD_RECOIL_STRENGTH, r.strength))
        time.sleep(0.1)
        self.ser.write(frame(CMD_RECOIL_REPEAT, 1 if r.repeat else 0))
        time.sleep(0.1)
        self.ser.write(frame(CMD_RECOIL_EVENTS, int(r.trigger), int(r.trigger_offscreen),
                             int(r.pump_on), int(r.pump_off)))
        time.sleep(0.1)
        self.ser.write(frame(CMD_RECOIL_BUTTONS, int(r.front_left), int(r.back_left),
                             int(r.front_right), int(r.back_right)))
        time.sleep(0.1)
        self.ser.write(frame(CMD_RECOIL_AB, 4, 4, 4, 0))
        time.sleep(0.1)
        self.ser.write(frame(CMD_RECOIL_ENABLE, 1 if (r.enabled and r.agree_terms) else 0, 4, 4, 0))
        time.sleep(0.1)
        self.ser.write(frame(CMD_RECOIL_VOLTAGE, min(255, int(round(r.strength * 2.5)))))
        time.sleep(0.1)

    def save_calibration(self, cal_x, cal_y):
        """Store the calibration (percent of the camera image) in the gun's memory."""
        for cmd, value in ((CMD_SAVE_CALIBRATION_X, cal_x), (CMD_SAVE_CALIBRATION_Y, cal_y)):
            raw = value * 100.0 + 10000.0
            raw = max(0.0, min(65535.0, raw))
            hi = int(raw // 256)
            lo = int(round(raw % 256)) & 0xFF
            self.send(cmd, hi, lo)
            time.sleep(0.1)
            self.ser.drain_input()
        self.calibration = (cal_x, cal_y)

    # -- runtime ---------------------------------------------------------
    def send_position(self, pct_x, pct_y, trigger_variant=False):
        """pct_x / pct_y in 0..100 (screen percentage)."""
        x = int(round(max(0.0, min(100.0, pct_x)) / 100.0 * 0x7FFF))
        y = int(round(max(0.0, min(100.0, pct_y)) / 100.0 * 0x7FFF))
        cmd = CMD_SET_POSITION_LOWRES_TRIGGER if trigger_variant else CMD_SET_POSITION
        self.send(cmd, x >> 8, x & 0xFF, y >> 8, y & 0xFF)
        return x, y

    def poll_events(self):
        """Read pending bytes from the gun and return a list of event tuples."""
        events = []
        data = self.ser.read_available()
        i = 0
        while i < len(data):
            b = data[i]
            if b == EVT_BUTTON_STATE:
                if i + 2 < len(data):
                    self.button_state = (data[i + 1], data[i + 2])
                    events.append(("buttons", self.button_state))
                    i += 3
                    continue
                # Incomplete: try to fetch the rest.
                rest = self.ser.read(2 - (len(data) - i - 1), timeout=0.02)
                tail = data[i + 1:] + rest
                if len(tail) >= 2:
                    self.button_state = (tail[0], tail[1])
                    events.append(("buttons", self.button_state))
                break
            elif b == EVT_ENTER_CALIBRATION:
                events.append(("enter_calibration",))
            elif b == EVT_CALIBRATION_SET:
                events.append(("calibration_set",))
                events.append(("exit_calibration",))
            elif b == EVT_EXIT_CALIBRATION:
                events.append(("exit_calibration",))
            elif b == EVT_TRIGGER_ENABLED:
                events.append(("trigger_enabled",))
            elif b == EVT_TRIGGER_DISABLED:
                events.append(("trigger_disabled",))
            i += 1
        return events
