# CLAUDE.md

OpenSinden: Python / Qt6 (PySide6) reimplementation of the Linux Sinden Lightgun driver
(`SindenLightgun/LightgunMono.exe`, .NET/Mono). Interface and messages in English.

## Safety rules (mandatory)

- **Never test recoil**: send no 0xA1–0xAC / 0xB5 command with an activation value, no
  "test recoil" button. For experiments, `EnableRecoil=0` → only `switch_off_recoil()` (A1 0)
  and B5 0 are sent.
- **Never update the firmware** (the feature is deliberately absent).
- `save_calibration()` ('j'/'k' commands) writes to the gun's memory: only call it when the user
  asks.
- When the gun is running and aiming at the screen, it **moves the real cursor** (the gun is the
  HID mouse). Stop experiments quickly; kill the GUI with SIGTERM/SIGINT (handled).

## Commands

```bash
.venv/bin/python -m opensinden ...   # or ./opensinden.sh ...
./opensinden.sh info                 # handshake + info (read-only, safe)
./opensinden.sh -v run               # headless driver, fps/time/position logs
./opensinden.sh gui [--start] [--tab N]
./opensinden.sh calibrate | border [--toggle|--stop]
QT_QPA_PLATFORM=offscreen ...        # GUI tests without a display
spectacle -b -n -f -o file.png       # fullscreen capture to check the GUI/border
```

For tests, use a temporary `--config` file rather than
`~/.config/opensinden/LightgunMono.exe.config` (the user's real config).

The `.venv/` venv (PySide6, numpy, opencv-python-headless) is based on `/usr/bin/python3.13`.
**Python >= 3.12 required** (guard in `__main__.py`): with pyenv's Python 3.10 (default
`python3`), PySide6 6.12 loses a reference to `None` on every signal emission → crash
"Fatal Python error: none_dealloc" after ~8000 events (seen when running MAME). To check for a
leak: compare `sys.getrefcount(None)` before/after N calls (no effect on 3.12+, `None` is
immortal).

## Architecture

- `protocol.py`: `AA cmd a b c d BB` frames, 115200 baud, DTR/RTS. Two-way SHA-256 handshake
  (salts `_SECRET_1[32:73]`, `_SECRET_2[32:64]`), then 'y' ×2. Position: 0x28 + X,Y big-endian
  over 0..32767. Received events: C8/C9/CA calibration, FE + 2 button bytes.
- `camera.py`: OpenCV V4L2 MJPEG + controls via ioctl. **Do not set
  `CAP_PROP_BUFFERSIZE=1`** (halves the fps).
- `vision.py`: colour mask (LUT, bit-identical to the original formula, reduced 2×2 "at least one
  pixel"), largest convex quadrilateral, homography to a **0..99** grid (like the original
  AForge, keeps existing calibrations compatible). Fixed sight offset 4.9%.
- `engine.py`: one thread per gun. Camera ↔ gun matched by parent USB hub. Original camera
  rules: name ('f' reply, spaces removed) of length 1–11 → camera upright; brightness −100 only
  for SindenCamA/B; SindenCamC → manual exposure 78.
- `borders.py`: layer-shell QML generated from `kde-screen-borders/`, launched by `qml6`
  (requires `layer-shell-qt6-imports`; `QML_IMPORT_PATH` is also honoured), otherwise a PySide6
  overlay. **The Qt overlay is hidden by fullscreen windows (MAME) and sometimes by the Plasma
  panel**: only the layer-shell "overlay" layer stays on top (checked with a `spectacle` capture
  + a fullscreen test window).
- `config.py`: `LightgunMono.exe.config` XML format (comments preserved); `P2` suffix for
  player 2; extra keys listed in `EXTRA_DEFAULTS`.

## Reverse engineering

IL disassembled with `mono /usr/lib/mono/4.5/ikdasm.exe SindenLightgun/LightgunMono.exe`.
Key methods: `HandshakeWithLightgun`, `SetupLightgunSettings`, `AssignButtons`,
`AssignRecoil`, `MoveMouse`, `ProcessImage`, `FilterImage2`, `GetXYPercentagesMatrix`,
`WorkOutCentreOfQuad`, `CameraSettings`, `ReadConfigValues`. Consult the IL before changing a
behaviour that must stay "faithful to the original".

## Test hardware

1 gun: `/dev/ttyACM0` (16c0:0f38, firmware v1.9), camera `SindenCamC` `/dev/video0`
(16d0:1097), same USB hub `1-1`. KDE Wayland session, 1920×1080 screen.
