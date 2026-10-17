"""Minimal POSIX serial port (termios) so we don't depend on pyserial."""

import fcntl
import os
import select
import struct
import termios
import time

TIOCM_DTR = 0x002
TIOCM_RTS = 0x004
TIOCMBIS = getattr(termios, "TIOCMBIS", 0x5416)
FIONREAD = getattr(termios, "FIONREAD", 0x541B)

_BAUDS = {
    9600: termios.B9600,
    57600: termios.B57600,
    115200: termios.B115200,
}


class SerialPort:
    def __init__(self, path, baudrate=115200):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            self._configure(baudrate)
        except Exception:
            os.close(self.fd)
            raise

    def _configure(self, baudrate):
        attrs = termios.tcgetattr(self.fd)
        iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
        iflag = 0
        oflag = 0
        lflag = 0
        cflag = termios.CS8 | termios.CREAD | termios.CLOCAL
        speed = _BAUDS.get(baudrate, termios.B115200)
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW,
                          [iflag, oflag, cflag, lflag, speed, speed, cc])
        # The official driver enables DTR and RTS.
        fcntl.ioctl(self.fd, TIOCMBIS, struct.pack("I", TIOCM_DTR | TIOCM_RTS))
        termios.tcflush(self.fd, termios.TCIOFLUSH)

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            finally:
                self.fd = None

    def write(self, data):
        data = bytes(data)
        view = memoryview(data)
        deadline = time.monotonic() + 2.0
        while view:
            try:
                n = os.write(self.fd, view)
                view = view[n:]
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TimeoutError("serial write timeout")
                select.select([], [self.fd], [], 0.1)

    def in_waiting(self):
        buf = fcntl.ioctl(self.fd, FIONREAD, b"\0\0\0\0")
        return struct.unpack("I", buf)[0]

    def read(self, n, timeout=1.0):
        """Read up to n bytes, waiting at most `timeout` seconds for them."""
        out = bytearray()
        deadline = time.monotonic() + timeout
        while len(out) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            r, _, _ = select.select([self.fd], [], [], remaining)
            if not r:
                break
            try:
                chunk = os.read(self.fd, n - len(out))
            except BlockingIOError:
                continue
            if not chunk:
                raise OSError("serial device disconnected")
            out += chunk
        return bytes(out)

    def read_available(self):
        n = self.in_waiting()
        if n <= 0:
            return b""
        try:
            return os.read(self.fd, n)
        except BlockingIOError:
            return b""

    def wait_for(self, n, timeout=2.0):
        """Wait until at least n bytes are buffered. Returns True on success."""
        deadline = time.monotonic() + timeout
        while self.in_waiting() < n:
            if time.monotonic() > deadline:
                return False
            time.sleep(0.005)
        return True

    def drain_input(self):
        while self.read_available():
            pass

    def readline(self, timeout=1.0):
        out = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            b = self.read(1, timeout=max(0.0, deadline - time.monotonic()))
            if not b:
                break
            if b == b"\n":
                break
            out += b
        return out.decode("ascii", "replace").rstrip("\r")
