"""Fake Arduino Uno running firmware/uno_safe_action: the same line protocol and watchdog, in Python, for
tests and laptop demos (pyserial socket:// in place of /dev/ttyACM0).

    python tools/fake_uno.py [--tcp-port 0]        # prints PORT socket://127.0.0.1:NNNN, then one line per change

Model (kept identical to the sketch): SAFE at power-up; every valid $GG line sets mode / servo / relay and
the watchdog period (clamped 1000..10000 ms) and is acknowledged with $GA,<seq>,<MODE>; no valid line for
the watchdog period -> SAFE (relay off, servo 180).
"""
from __future__ import annotations

import argparse
import socket
import time

SAFE_DEG = 180


def xorsum(s: str) -> str:
    x = 0
    for ch in s:
        x ^= ord(ch)
    return f"{x:02X}"


def line(body: str) -> bytes:
    return f"${body}*{xorsum(body)}\r\n".encode("ascii")


class FakeUno:
    def __init__(self, now: float = 0.0):
        self.mode, self.servo, self.relay = "SAFE", SAFE_DEG, False
        self.watchdog_ms = 3000
        self.last_valid = now
        self.buf = b""
        self.history: list[tuple[float, str, int, bool]] = [(now, "SAFE", SAFE_DEG, False)]
        self.banner = line("GR,uno_safe_action,1")

    def _set(self, now, mode, servo, relay):
        if (mode, servo, relay) != (self.mode, self.servo, self.relay):
            self.history.append((now, mode, servo, relay))
        self.mode, self.servo, self.relay = mode, servo, relay

    def feed(self, data: bytes, now: float) -> bytes:
        """Bytes from the Pi; returns the acknowledgements."""
        out = b""
        self.buf += data
        *lines, self.buf = self.buf.replace(b"\r", b"\n").split(b"\n")
        for raw in lines:
            s = raw.decode("ascii", "replace").strip()
            if not s.startswith("$") or "*" not in s:
                continue
            body, cs = s[1:s.rindex("*")], s[s.rindex("*") + 1:]
            if xorsum(body) != cs.upper()[:2]:
                continue
            f = body.split(",")
            if len(f) < 6 or f[0] != "GG":
                continue
            try:
                deg, relay, wd = max(0, min(180, int(f[3]))), f[4] == "1", int(f[5])
            except ValueError:
                continue
            self.watchdog_ms = max(1000, min(10000, wd))
            if f[2] == "SAFE":
                self._set(now, "SAFE", SAFE_DEG, False)
            else:
                self._set(now, f[2], deg, relay)
            self.last_valid = now
            out += line(f"GA,{f[1]},{f[2]}")
        return out

    def tick(self, now: float) -> None:
        if self.mode != "SAFE" and (now - self.last_valid) * 1000 > self.watchdog_ms:
            self._set(now, "SAFE", SAFE_DEG, False)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tcp-port", type=int, default=0)
    ap.add_argument("--duration-s", type=float, default=None)
    a = ap.parse_args(argv)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", a.tcp_port))
    srv.listen(1)
    srv.settimeout(0.05)
    print(f"PORT socket://127.0.0.1:{srv.getsockname()[1]}", flush=True)
    uno, cli, t0 = FakeUno(time.monotonic()), None, time.monotonic()
    shown = None
    try:
        while a.duration_s is None or time.monotonic() - t0 < a.duration_s:
            now = time.monotonic()
            if cli is None:
                try:
                    cli, _ = srv.accept()
                    cli.settimeout(0.05)
                    uno = FakeUno(now)                 # opening the port resets the Uno (DTR)
                    cli.sendall(uno.banner)
                except (socket.timeout, OSError):
                    pass
            else:
                try:
                    data = cli.recv(4096)
                    if not data:
                        cli.close()
                        cli = None
                    else:
                        ack = uno.feed(data, now)
                        if ack:
                            cli.sendall(ack)
                except socket.timeout:
                    pass
                except OSError:
                    cli = None
            uno.tick(time.monotonic())
            cur = (uno.mode, uno.servo, uno.relay)
            if cur != shown:
                shown = cur
                print(f"UNO {time.monotonic() - t0:7.2f}s mode {uno.mode} servo {uno.servo} relay {int(uno.relay)}",
                      flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()


if __name__ == "__main__":
    main()
