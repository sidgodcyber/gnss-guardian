"""Demo spoof injection (software only, no RF): rewrites the position in the real L86 NMEA stream
before the detector, for a field demo where no spoofer is available.

    inj = DemoInjector()
    inj.start("jump", bearing_deg=90, m=30)        # position offset N m at bearing B, from now
    inj.start("drift", bearing_deg=90, mps=0.5)    # offset growing at V m/s along B (reported speed follows)
    line = inj.apply(line)                         # every NMEA line, after the raw log, before the pipeline
    inj.stop()

Only RMC, GGA, GLL (position) and RMC/VTG (speed, course, for a drift) of fixes are changed; the
satellites, C/N0, HDOP and time stay real. The detector and the state machine see only the modified
stream and get no flag that it is injected: the flag lives here, in the session's demo_inject events,
in live.json and on the overlay and guidance page, so no one mistakes the demo for a real attack.
"""
from __future__ import annotations

import math
import threading
import time

from gnss_guardian import geo
from gnss_guardian.io.nmea import KNOT

KINDS = ("jump", "drift")


def _checksum(body: str) -> str:
    x = 0
    for ch in body:
        x ^= ord(ch)
    return f"{x:02X}"


def _parse_ll(v: str, hemi: str) -> float | None:
    try:
        dot = v.index(".")
        d = float(v[:dot - 2]) + float(v[dot - 2:]) / 60
    except ValueError:
        return None
    return -d if hemi in "SW" else d


def _format_ll(x: float, deg_width: int, decimals: int) -> tuple[str, str]:
    a = abs(x)
    d = int(a)
    m = round((a - d) * 60, decimals)
    if m >= 60:
        d, m = d + 1, 0.0
    hemi = ("N" if x >= 0 else "S") if deg_width == 2 else ("E" if x >= 0 else "W")
    return f"{d:0{deg_width}d}{m:0{3 + decimals}.{decimals}f}", hemi


class DemoInjector:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self.active: dict | None = None
        self.count = 0

    # ---------------------------------------------------------------- control (HTTP thread)
    def start(self, kind: str, bearing_deg: float, m: float | None = None, mps: float | None = None) -> dict:
        if kind not in KINDS:
            raise ValueError(f"injection {kind!r}: use {' or '.join(KINDS)}")
        if kind == "jump" and not (m is not None and 0 < m <= 5000):
            raise ValueError("jump needs m in (0, 5000]")
        if kind == "drift" and not (mps is not None and 0 < mps <= 20):
            raise ValueError("drift needs mps in (0, 20]")
        if not math.isfinite(bearing_deg):
            raise ValueError("bearing must be a number of degrees")
        with self._lock:
            self.count += 1
            self.active = {"id": self.count, "type": kind, "bearing_deg": round(bearing_deg % 360, 1),
                           "m": m if kind == "jump" else None, "mps": mps if kind == "drift" else None,
                           "t0": self.clock()}
            return self._public()

    def stop(self) -> dict | None:
        with self._lock:
            was = self._public()
            self.active = None
            return was

    def status(self) -> dict:
        with self._lock:
            return {"enabled": True, "active": self._public()}

    def _public(self) -> dict | None:
        a = self.active
        if a is None:
            return None
        de, dn, *_ = self._offset()
        return {k: v for k, v in a.items() if k != "t0"} | {"elapsed_s": round(self.clock() - a["t0"], 1),
                                                           "offset_m": round(math.hypot(de, dn), 1)}

    def _offset(self) -> tuple[float, float, float, float]:
        """(east, north) metres added to the position and (v_east, v_north) m/s added to the velocity."""
        a = self.active
        if a is None:
            return 0.0, 0.0, 0.0, 0.0
        ue, un = math.sin(math.radians(a["bearing_deg"])), math.cos(math.radians(a["bearing_deg"]))
        if a["type"] == "jump":
            return a["m"] * ue, a["m"] * un, 0.0, 0.0
        d = a["mps"] * (self.clock() - a["t0"])
        return d * ue, d * un, a["mps"] * ue, a["mps"] * un

    # ---------------------------------------------------------------- stream (serial thread)
    def apply(self, line: str) -> str:
        if self.active is None or not line.startswith("$") or "*" not in line:
            return line
        with self._lock:
            if self.active is None:
                return line
            de, dn, ve, vn = self._offset()
        body = line[1:line.rindex("*")]
        f = body.split(",")
        stype = f[0][2:]
        if stype == "RMC" and len(f) > 8 and f[2] == "A":
            self._move(f, 3, de, dn)
            if ve or vn:
                self._velocity(f, 7, 8, ve, vn)
        elif stype == "GGA" and len(f) > 6 and f[6] not in ("", "0"):
            self._move(f, 2, de, dn)
        elif stype == "GLL" and len(f) > 6 and f[6] == "A":
            self._move(f, 1, de, dn)
        elif stype == "VTG" and len(f) > 7 and (ve or vn):
            self._velocity(f, 5, 1, ve, vn)
            if f[5]:
                f[7] = f"{float(f[5]) * 1.852:.2f}"
        else:
            return line
        body = ",".join(f)
        return f"${body}*{_checksum(body)}"

    @staticmethod
    def _move(f: list[str], i: int, de: float, dn: float) -> None:
        lat, lon = _parse_ll(f[i], f[i + 1]), _parse_ll(f[i + 2], f[i + 3])
        if lat is None or lon is None:
            return
        lat2, lon2 = geo.from_enu(de, dn, lat, lon)
        dec = len(f[i].split(".")[1]) if "." in f[i] else 4
        f[i], f[i + 1] = _format_ll(lat2, 2, dec)
        f[i + 2], f[i + 3] = _format_ll(lon2, 3, dec)

    @staticmethod
    def _velocity(f: list[str], i_sog: int, i_cog: int, ve: float, vn: float) -> None:
        try:
            sog = float(f[i_sog] or 0) * KNOT
            cog = math.radians(float(f[i_cog] or 0))
        except ValueError:
            return
        e, n = sog * math.sin(cog) + ve, sog * math.cos(cog) + vn
        f[i_sog] = f"{math.hypot(e, n) / KNOT:.2f}"
        f[i_cog] = f"{math.degrees(math.atan2(e, n)) % 360:.2f}"
