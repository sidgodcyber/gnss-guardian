#!/usr/bin/env python3
"""Record raw L86 NMEA with a host-time prefix, for replay and test fixtures. Standalone: pyserial only.

    python3 record_nmea.py --out clean.nmea [--port /dev/serial0] [--baud 9600] [--duration-min 30]

Each line is written as ``2026-10-23T08:15:02.123Z,$GPRMC,...*hh`` (host UTC receive time, the
IonWatch RawNmeaLog format), so gnss-guardian replay can compare GNSS time with the host clock.
Survives unplug/replug and a silent port (reopens after --silence-reopen-s). A status line
every 10 s shows that data is arriving: lines, RMC fix status, satellites with C/N0, max C/N0.
--port also takes pyserial URLs (socket://127.0.0.1:5000) for testing against tools/fake_l86.py.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

import serial


def prefix(t: datetime) -> str:
    return f"{t:%Y-%m-%dT%H:%M:%S}.{t.microsecond // 1000:03d}Z"


class Stats:
    def __init__(self):
        self.lines, self.bad, self.fix, self.reopens = 0, 0, "-", 0
        self.cn0: dict[str, int] = {}

    def update(self, line: str) -> None:
        self.lines += 1
        if not line.startswith("$") or "*" not in line:
            self.bad += 1
            return
        f = line[1:line.rindex("*")].split(",")
        kind = f[0][2:]
        if kind == "RMC" and len(f) > 2:
            self.fix = f[2] or "-"
            self.cn0 = {}                         # new fix cycle
        elif kind == "GSV":
            for i in range(4, len(f) - 3, 4):
                if f[i + 3].strip().isdigit() and int(f[i + 3]) > 0:
                    self.cn0[f[0][:2] + f[i]] = int(f[i + 3])

    def line(self, elapsed_s: float, total_s: float) -> str:
        mx = max(self.cn0.values()) if self.cn0 else "-"
        return (f"{prefix(datetime.now(timezone.utc))} | {elapsed_s / 60:5.1f}/{total_s / 60:g} min | "
                f"lines {self.lines} (bad {self.bad}) | RMC {self.fix} | sats w/ C/N0 {len(self.cn0)} | "
                f"max C/N0 {mx} | reopens {self.reopens}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="output file (appended to if it exists)")
    ap.add_argument("--port", default="/dev/serial0")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--duration-min", type=float, default=30.0)
    ap.add_argument("--silence-reopen-s", type=float, default=10.0, help="no bytes for this long: reopen the port")
    ap.add_argument("--status-every-s", type=float, default=10.0)
    a = ap.parse_args(argv)

    total = a.duration_min * 60
    st = Stats()
    t0 = time.monotonic()
    t_status = t0
    ser, pending, last_bytes = None, b"", time.monotonic()
    print(f"recording {a.port} @ {a.baud} -> {a.out} for {a.duration_min:g} min (Ctrl-C stops early)", flush=True)
    with open(a.out, "a", encoding="ascii", errors="replace", newline="\n") as out:
        try:
            while time.monotonic() - t0 < total:
                if ser is None:
                    try:
                        ser = serial.serial_for_url(a.port, baudrate=a.baud, timeout=0.2)
                        pending, last_bytes = b"", time.monotonic()
                        print(f"{prefix(datetime.now(timezone.utc))} port open", flush=True)
                    except (serial.SerialException, OSError, ValueError) as e:
                        print(f"{prefix(datetime.now(timezone.utc))} cannot open {a.port}: {e}; retry in 2 s",
                              flush=True)
                        time.sleep(2)
                        continue
                try:
                    chunk = ser.read(4096)
                except (serial.SerialException, OSError) as e:
                    print(f"{prefix(datetime.now(timezone.utc))} port lost: {e}; reopening", flush=True)
                    try:
                        ser.close()
                    except Exception:
                        pass
                    ser = None
                    st.reopens += 1
                    time.sleep(1)
                    continue
                now = datetime.now(timezone.utc)
                if chunk:
                    last_bytes = time.monotonic()
                    pending += chunk
                    *lines, pending = pending.split(b"\n")
                    if len(pending) > 1024:              # no newline for 1 kB: garbage
                        pending = b""
                    for bl in lines:
                        line = bl.decode("ascii", errors="replace").strip()
                        if line:
                            out.write(f"{prefix(now)},{line}\n")
                            st.update(line)
                    out.flush()
                elif time.monotonic() - last_bytes > a.silence_reopen_s:
                    # a GPIO UART does not error when the L86 is unplugged; it just goes quiet
                    print(f"{prefix(now)} no bytes for {a.silence_reopen_s:g} s (L86 powered? wired?); reopening",
                          flush=True)
                    ser.close()
                    ser = None
                    st.reopens += 1
                    continue
                if time.monotonic() - t_status >= a.status_every_s:
                    t_status = time.monotonic()
                    print(st.line(t_status - t0, total), flush=True)
        except KeyboardInterrupt:
            print("stopped by user", flush=True)
        finally:
            if ser is not None:
                ser.close()
    print(f"done: {st.lines} lines in {a.out}", flush=True)
    return 0 if st.lines else 1


if __name__ == "__main__":
    sys.exit(main())
