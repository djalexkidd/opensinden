"""Read / write settings in the official LightgunMono.exe.config XML format.

The file keeps the official keys (so it stays interchangeable with the mono
driver) plus a few extra keys used by the border overlay and the GUI.
"""

import os
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass

DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".config", "opensinden")
DEFAULT_PATH = os.path.join(DEFAULT_DIR, "LightgunMono.exe.config")
_HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(_HERE, "LightgunMono.exe.config")

# Extra keys (not used by the official Linux driver).
EXTRA_DEFAULTS = {
    "BorderEnabled": "1",
    "BorderColourRGB": "255,255,255",
    "BorderWidthPercent": "2",
    "OuterBorderEnabled": "0",
    "OuterBorderColourRGB": "0,0,0",
    "OuterBorderWidthPercent": "3",
    "BorderDisplay": "0",
    "BorderMethod": "auto",
    "AntiJitter": "0",
    "AntiJitterPercent": "1",
    "JoystickMode": "0",
    "LowResource": "0",
    "CameraSaturation": "40",
    "CameraHue": "0",
    "CameraWhiteBalanceAuto": "0",
    "CameraWhiteBalance": "4500",
    "CameraZoom": "",
    "CameraGain": "",
}

BUTTON_NAMES = ["Trigger", "PumpAction", "FrontLeft", "RearLeft", "FrontRight",
                "RearRight", "Up", "Down", "Left", "Right"]


class Config:
    def __init__(self, path=DEFAULT_PATH):
        self.path = path
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            shutil.copy(TEMPLATE_PATH, path)
        self.load()

    def load(self):
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
        self.tree = ET.parse(self.path, parser=parser)
        root = self.tree.getroot()
        self.settings = root.find("appSettings")
        if self.settings is None:
            self.settings = ET.SubElement(root, "appSettings")

    def _find(self, key):
        for el in self.settings.findall("add"):
            if el.get("key") == key:
                return el
        return None

    def get(self, key, default=""):
        el = self._find(key)
        if el is None:
            return EXTRA_DEFAULTS.get(key, default)
        return el.get("value", default)

    def get_int(self, key, default=0):
        try:
            return int(float(self.get(key, str(default)) or default))
        except ValueError:
            return default

    def get_float(self, key, default=0.0):
        try:
            return float((self.get(key, str(default)) or str(default)).replace(",", "."))
        except ValueError:
            return default

    def get_bool(self, key, default=False):
        return self.get(key, "1" if default else "0") == "1"

    def get_rgb(self, key, default=(255, 255, 255)):
        try:
            parts = [int(p) for p in self.get(key).split(",")]
            return tuple(max(0, min(255, p)) for p in parts[:3])
        except ValueError:
            return default

    def set(self, key, value):
        if isinstance(value, bool):
            value = "1" if value else "0"
        value = str(value)
        el = self._find(key)
        if el is None:
            el = ET.SubElement(self.settings, "add", {"key": key, "value": value})
            el.tail = "\n    "
        else:
            el.set("value", value)

    def save(self):
        self.tree.write(self.path, encoding="utf-8", xml_declaration=True)


@dataclass
class RecoilSettings:
    agree_terms: bool = False
    enabled: bool = False
    trigger: bool = True
    trigger_offscreen: bool = False
    pump_on: bool = False
    pump_off: bool = False
    front_left: bool = False
    front_right: bool = False
    back_left: bool = False
    back_right: bool = False
    strength: int = 100
    repeat: bool = False
    auto_strength: int = 40
    auto_start_delay: int = 0
    auto_delay_between_pulses: int = 13


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


class PlayerConfig:
    """View of the config for one player (suffix '' for P1, 'P2' for P2)."""

    def __init__(self, cfg: Config, player=1):
        self.cfg = cfg
        self.player = player
        self.sfx = "" if player == 1 else "P%d" % player

    def k(self, key):
        return key + self.sfx

    def get(self, key, default=""):
        return self.cfg.get(self.k(key), default)

    @property
    def serial_index(self):
        v = self.get("SerialPortWrite", str(self.player - 1))
        return v

    @property
    def video_device(self):
        return self.get("VideoDevice")

    def calibration_override(self):
        x, y = self.get("CalibrateX"), self.get("CalibrateY")
        cx = float(x.replace(",", ".")) if x.strip() else None
        cy = float(y.replace(",", ".")) if y.strip() else None
        return cx, cy

    @property
    def gangsta(self):
        return self.get("GangstaSetting", "2")

    @property
    def offscreen_reload(self):
        return self.get("OffscreenReload") == "1"

    def recoil(self):
        g = lambda key: self.get(key) == "1"
        gi = lambda key, d: int(self.get(key, str(d)) or d)
        return RecoilSettings(
            agree_terms=g("IAgreeRecoilTermsInLicense"),
            enabled=g("EnableRecoil"),
            trigger=g("RecoilTrigger"),
            trigger_offscreen=g("RecoilTriggerOffscreen"),
            pump_on=g("RecoilPumpActionOnEvent"),
            pump_off=g("RecoilPumpActionOffEvent"),
            front_left=g("RecoilFrontLeft"),
            front_right=g("RecoilFrontRight"),
            back_left=g("RecoilBackLeft"),
            back_right=g("RecoilBackRight"),
            strength=_clamp(gi("RecoilStrength", 100), 0, 100),
            repeat=g("TriggerRecoilNormalOrRepeat"),
            auto_strength=_clamp(gi("AutoRecoilStrength", 40), 0, 200),
            auto_start_delay=_clamp(gi("AutoRecoilStartDelay", 0), 0, 200),
            auto_delay_between_pulses=_clamp(gi("AutoRecoilDelayBetweenPulses", 13), 0, 200),
        )

    def button_actions(self, joystick_mode):
        """Return (actions, modifiers) dicts keyed by firmware slot name."""
        pre = "JoystickMode" if joystick_mode else ""
        actions, mods = {}, {}
        for name in BUTTON_NAMES:
            for off in ("", "Offscreen"):
                slot = name + off
                actions[slot] = self.cfg.get_int(pre + "Button" + name + off + self.sfx, 0)
                mods[slot] = self.cfg.get_int(pre + name + off + "Mod" + self.sfx, 0)
        return actions, mods
