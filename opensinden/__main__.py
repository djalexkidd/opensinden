"""OpenSinden command line.

  opensinden                     GUI (Qt6)
  opensinden gui [--start]       GUI, optionally starting the guns immediately
  opensinden run [--joystick]    headless driver (like `mono-service LightgunMono.exe`)
  opensinden calibrate           fullscreen calibration / test screen (like the `sdl` mode)
  opensinden border [--toggle|--stop]
  opensinden info                handshake with the guns and print their info
"""

import argparse
import logging
import signal
import sys
import time


def cmd_info(args):
    from . import protocol
    from .engine import camera_for_port
    ports = protocol.find_lightgun_ports()
    if not ports:
        print("No Sinden Lightgun found.")
        return 1
    for i, port in enumerate(ports):
        g = protocol.Lightgun(port)
        try:
            g.handshake()
            name = g.read_camera_name()
            cal = g.read_calibration()
            fw = g.read_firmware_version()
            print("P%d %s: camera=%s (%s) firmware=v%d.%d calibration X=%.2f Y=%.2f" % (
                i + 1, port, name, camera_for_port(port, name), fw[0], fw[1], cal[0], cal[1]))
        finally:
            g.close()
    return 0


def cmd_run(args):
    from .config import Config
    from .engine import Engine
    cfg = Config(args.config) if args.config else Config()
    joystick = True if args.joystick else None
    engine = Engine(cfg, joystick)
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    engine.start()
    try:
        last = 0
        while not stop and engine.running:
            time.sleep(0.2)
            if args.verbose and time.time() - last > 2:
                last = time.time()
                for t in engine.trackers:
                    i = t.info()
                    logging.info("%s %s fps=%.1f proc=%.1fms found=%d%% pos=%s",
                                 i["name"], i["status"], i["fps"], i["process_ms"],
                                 i["found_ratio"] * 100, i["last_sent"])
    finally:
        engine.stop()
    return 0


def main(argv=None):
    if sys.version_info < (3, 12):
        # PySide6 6.12 corrupts None's refcount (and the heap) on older Pythons:
        # signal emits slowly drain None until the interpreter aborts.
        sys.exit("OpenSinden requires Python >= 3.12 (found %s). Recreate the venv with:\n"
                 "  rm -rf .venv && /usr/bin/python3.13 -m venv .venv && "
                 ".venv/bin/pip install PySide6 numpy opencv-python-headless" % sys.version.split()[0])
    p = argparse.ArgumentParser(prog="opensinden", description="Open source Sinden Lightgun driver for Linux")
    p.add_argument("--config", help="path of the LightgunMono.exe.config style file")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd")
    g = sub.add_parser("gui")
    g.add_argument("--start", action="store_true", help="start the lightguns immediately")
    g.add_argument("--joystick", action="store_true")
    g.add_argument("--tab", type=int, default=0, help="initial tab index")
    r = sub.add_parser("run")
    r.add_argument("--joystick", action="store_true", help="joystick mode (otherwise from config)")
    c = sub.add_parser("calibrate")
    c.add_argument("--joystick", action="store_true")
    b = sub.add_parser("border")
    grp = b.add_mutually_exclusive_group()
    grp.add_argument("--toggle", action="store_true")
    grp.add_argument("--stop", action="store_true")
    sub.add_parser("info")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose and args.cmd != "run" else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    if args.cmd == "info":
        return cmd_info(args)
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "border":
        from . import borders
        if args.stop:
            borders.stop_standalone()
            return 0
        return borders.run_standalone(args.config, toggle=args.toggle)
    if args.cmd == "calibrate":
        from PySide6 import QtCore, QtWidgets
        from .gui import MainWindow, install_signal_handlers
        from .config import Config
        app = QtWidgets.QApplication(sys.argv[:1])
        cfg = Config(args.config) if args.config else Config()
        win = MainWindow(cfg, True if args.joystick else None)
        install_signal_handlers(win.quit)
        win.start()
        win.open_calibration_screen()
        win.calib_screen.destroyed.connect(win.quit)
        win.calib_screen.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        return app.exec()
    from .gui import run_gui
    joystick = True if getattr(args, "joystick", False) else None
    return run_gui(args.config, joystick, autostart=getattr(args, "start", False),
                   tab=getattr(args, "tab", 0))


if __name__ == "__main__":
    sys.exit(main())
