# OpenSinden

Python reimplementation (Qt6 / PySide6 interface) of the Sinden Lightgun software for Linux.

The gun is itself the USB mouse / keyboard / joystick: the software reads the gun's camera,
detects the white border around the screen, computes where the gun is aiming and sends the
position to the firmware over the serial port (`/dev/ttyACM*`).

## Installation

```bash
git clone https://github.com/djalexkidd/opensinden.git OpenSinden && cd OpenSinden
/usr/bin/python3.13 -m venv .venv   # Python >= 3.12 required
.venv/bin/pip install PySide6 numpy opencv-python-headless
```

Do not use the pyenv Python 3.10: with PySide6 6.12 it corrupts `None`'s reference count and the
program crashes after a few minutes ("Fatal Python error: none_dealloc").

The user must belong to the `dialout` (serial port) and `video` (camera) groups.

For the layer-shell border (above everything, even fullscreen games):

```bash
sudo zypper install layer-shell-qt6-imports
```

Without this package, the border is drawn by a transparent "always on top" Qt window.

## Usage

```bash
./opensinden.sh                 # graphical interface
./opensinden.sh gui --start     # interface + start the gun immediately
./opensinden.sh run             # headless driver (equivalent of the mono-service LightgunMono.exe)
./opensinden.sh run --joystick  # same, in joystick mode
./opensinden.sh calibrate       # fullscreen calibration/test screen (equivalent of the "sdl" mode)
./opensinden.sh border          # show the border only; --toggle / --stop
./opensinden.sh info            # gun information (camera, firmware, calibration)
```

Calibration: with the border displayed, hold D-pad LEFT for 3 s, then shoot the cursor at the
centre. The calibration is computed by the software and then saved in the gun.

Global shortcut for the border: System Settings → Shortcuts → add a command
`/path/to/OpenSinden/opensinden.sh border --toggle`.

## Configuration

`~/.config/opensinden/LightgunMono.exe.config`, in the same format as the official file
(copied from `SindenLightgun/LightgunMono.exe.config` on first launch), with a few extra keys
(border, anti-jitter, joystick mode…). Use `--config` for another file.

## Layout

| File | Role |
|---|---|
| `opensinden/protocol.py` | serial protocol (handshake, settings, buttons, position, events) |
| `opensinden/camera.py` | V4L2 capture (MJPEG 640×480, 60 fps) and camera controls via ioctl |
| `opensinden/vision.py` | border detection and perspective transform → screen % |
| `opensinden/engine.py` | camera → vision → serial loop, one thread per gun (2 max) |
| `opensinden/borders.py` | white border (layer-shell QML from kde-screen-borders, or Qt) |
| `opensinden/gui.py` | Qt6 interface |

## AI disclosure

This project was made with Claude Code.
