"""Demo spoof injection (software only, no RF): rewrites the position in the real L86 NMEA stream
before the detector, for a field demo where no spoofer is available.

    inj = DemoInjector()
    inj.start("jump", bearing_deg=90, m=30)        # position offset N m at bearing B, from now
    inj.start("drift", bearing_deg=90, mps=0.5)    # offset growing at V m/s along B (reported speed follows)
    inj.start("sky", rot_deg=15, ghosts=2)         # wrong sky: GPS azimuths rotated, below-horizon PRNs added
    for out in inj.feed(line): ...                 # every NMEA line, after the raw log, before the pipeline
    inj.stop()

jump / drift: only RMC, GGA, GLL (position) and RMC/VTG (speed, course, for a drift) of fixes are
changed; the satellites, C/N0, HDOP and time stay real.

sky: only the GPS satellite list (the $GPGSV group) is changed; position, speed, time and the other
constellations stay real. Every GPS azimuth is rotated by rot_deg, and ``ghosts`` PRNs that the almanac
puts below -10 deg at the last real fix (chosen at start) are added at elevations 25/40/55 deg with a
C/N0 that follows elevation like a real satellite. The group is buffered until its last sentence and
re-emitted with new counts and checksums, so ``feed`` returns zero or more lines per line. This is the
wrong sky of a spoofer replaying an old or foreign recording: the position does not move, so the
camera sees nothing wrong; the sky check (detect/sky.py) does. The detector and the state machine see only the modified
stream and get no flag that it is injected: the flag lives here, in the session's demo_inject events,
in live.json and on the overlay and guidance page, so no one mistakes the demo for a real attack.
"""
from __future__ import annotations

import math
import threading
import time

from datetime import datetime, timezone

from gnss_guardian import geo
from gnss_guardian.io.nmea import KNOT

KINDS = ("jump", "drift", "sky")
GHOST_BELOW_DEG = -10.0                   # ghosts: PRNs at least this far below the horizon at the last fix
GHOST_ELEV = (25, 40, 55)
MAX_GHOSTS = 4


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


def label(a: dict) -> str:
    """One line for the banners: 'jump 30 m at 90 deg', 'sky: GPS azimuths +15 deg, 2 ghost satellites'."""
    if a["type"] == "jump":
        return f"jump {a['m']:g} m at {a['bearing_deg']:g} deg"
    if a["type"] == "drift":
        return f"drift {a['mps']:g} m/s at {a['bearing_deg']:g} deg"
    g = a.get("ghost_prns") or []
    return (f"sky: GPS azimuths +{a['rot_deg']:g} deg, {len(g)} ghost satellite{'' if len(g) == 1 else 's'}"
            + (f" ({' '.join(g)})" if g else ""))


class DemoInjector:
    def __init__(self, clock=time.monotonic, almanac: dict | None = None, leap_s: float = 18.0):
        self.clock = clock
        self.almanac = almanac                  # {prn: AlmanacEntry} for the sky ghosts (detect/sky.py)
        self.leap_s = leap_s
        self._lock = threading.Lock()
        self.active: dict | None = None
        self.count = 0
        self._fix: dict = {}                    # last real fix in the stream: lat, lon, alt, t (POSIX UTC)
        self._gsv: list[str] = []               # $GPGSV group being buffered (sky)
        self._n_groups = 0

    # ---------------------------------------------------------------- control (HTTP thread)
    def start(self, kind: str, bearing_deg: float = 0.0, m: float | None = None, mps: float | None = None,
              rot_deg: float | None = None, ghosts: int | None = None) -> dict:
        if kind not in KINDS:
            raise ValueError(f"injection {kind!r}: use {', '.join(KINDS)}")
        if kind == "jump" and not (m is not None and 0 < m <= 5000):
            raise ValueError("jump needs m in (0, 5000]")
        if kind == "drift" and not (mps is not None and 0 < mps <= 20):
            raise ValueError("drift needs mps in (0, 20]")
        if not math.isfinite(bearing_deg):
            raise ValueError("bearing must be a number of degrees")
        prns: list[str] = []
        if kind == "sky":
            rot_deg = 15.0 if rot_deg is None else float(rot_deg)
            ghosts = 2 if ghosts is None else int(ghosts)
            if not (math.isfinite(rot_deg) and 0 <= rot_deg <= 180):
                raise ValueError("sky needs rot in [0, 180] deg")
            if not 0 <= ghosts <= MAX_GHOSTS:
                raise ValueError(f"sky needs ghosts in [0, {MAX_GHOSTS}]")
            if rot_deg == 0 and ghosts == 0:
                raise ValueError("sky needs a rotation or at least one ghost satellite")
            if ghosts:
                prns = self._pick_ghosts(ghosts)
        with self._lock:
            self.count += 1
            self.active = {"id": self.count, "type": kind,
                           "bearing_deg": None if kind == "sky" else round(bearing_deg % 360, 1),
                           "m": m if kind == "jump" else None, "mps": mps if kind == "drift" else None,
                           "t0": self.clock()}
            if kind == "sky":
                self.active.update(rot_deg=round(rot_deg, 1), ghosts=ghosts, ghost_prns=prns)
            return self._public()

    def _pick_ghosts(self, n: int) -> list[str]:
        """The n GPS PRNs deepest below the horizon (< GHOST_BELOW_DEG) at the last real fix, now."""
        from gnss_guardian.detect.sky import predict
        if not self.almanac:
            raise ValueError("ghost satellites need the GPS almanac (detect.sky.almanac_file); use ghosts=0")
        f = self._fix
        if "lat" not in f:
            raise ValueError("no GNSS fix seen yet: ghosts are chosen from the almanac at the current position")
        pred = predict(self.almanac, f["t"], f["lat"], f["lon"], f.get("alt", 0.0), leap_s=self.leap_s)
        below = sorted((el, prn) for prn, (el, _) in pred.items() if el < GHOST_BELOW_DEG)
        if len(below) < n:
            raise ValueError(f"only {len(below)} almanac satellites below {GHOST_BELOW_DEG:g} deg here and now")
        return [f"G{prn:02d}" for prn in sorted(prn for _, prn in below[:n])]

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
                                                           "offset_m": round(math.hypot(de, dn), 1),
                                                           "label": label(a)}

    def _offset(self) -> tuple[float, float, float, float]:
        """(east, north) metres added to the position and (v_east, v_north) m/s added to the velocity."""
        a = self.active
        if a is None or a["type"] == "sky":
            return 0.0, 0.0, 0.0, 0.0
        ue, un = math.sin(math.radians(a["bearing_deg"])), math.cos(math.radians(a["bearing_deg"]))
        if a["type"] == "jump":
            return a["m"] * ue, a["m"] * un, 0.0, 0.0
        d = a["mps"] * (self.clock() - a["t0"])
        return d * ue, d * un, a["mps"] * ue, a["mps"] * un

    # ---------------------------------------------------------------- stream (serial thread)
    def feed(self, line: str) -> list[str]:
        """Every NMEA line in order; returns the lines the detector gets: for a sky injection nothing
        while a $GPGSV group is buffered, then the whole rewritten group at its last sentence."""
        self._watch(line)
        a = self.active
        if a is None or a["type"] != "sky":
            return self._release() + [self.apply(line)]      # stopped mid-group: pass it on as received
        if not (line.startswith("$GPGSV,") and "*" in line):
            return self._release() + [line]
        f = line[1:line.rindex("*")].split(",")
        try:
            total, num = int(f[1]), int(f[2])
        except (ValueError, IndexError):
            return self._release() + [line]
        out = self._release() if num == 1 else []
        self._gsv.append(line)
        if num < total:
            return out
        return out + self._rewrite(self._release(), a)

    def _release(self) -> list[str]:
        held, self._gsv = self._gsv, []
        return held

    def _watch(self, line: str) -> None:
        """Keep the last real fix (position, altitude, UTC) for choosing the ghost satellites."""
        if not (line.startswith("$") and "*" in line):
            return
        f = line[1:line.rindex("*")].split(",")
        stype = f[0][2:]
        try:
            if stype == "RMC" and len(f) > 9 and f[2] == "A":
                lat, lon = _parse_ll(f[3], f[4]), _parse_ll(f[5], f[6])
                t = datetime.strptime(f[9] + f[1].split(".")[0], "%d%m%y%H%M%S").replace(tzinfo=timezone.utc)
                if lat is not None and lon is not None:
                    self._fix.update(lat=lat, lon=lon, t=t.timestamp())
            elif stype == "GGA" and len(f) > 9 and f[6] not in ("", "0") and f[9]:
                self._fix["alt"] = float(f[9])
        except ValueError:
            pass

    def _rewrite(self, group: list[str], a: dict) -> list[str]:
        """One complete $GPGSV group with the azimuths rotated and the ghosts added, re-split by 4."""
        sats, sig = [], None
        for line in group:
            body = line[1:line.rindex("*")].split(",")[4:]
            if len(body) % 4 == 1:                            # NMEA 4.10 signal ID at the end
                sig, body = body[-1], body[:-1]
            for i in range(0, len(body) - 3, 4):
                prn, el, az, snr = body[i:i + 4]
                if not prn:
                    continue
                if a["rot_deg"] and el and az:
                    try:
                        az = f"{(int(float(az)) + int(round(a['rot_deg']))) % 360:03d}"
                    except ValueError:
                        pass
                sats.append([prn, el, az, snr])
        have = {s[0].lstrip("0") for s in sats}
        self._n_groups += 1
        for i, g in enumerate(a.get("ghost_prns") or []):
            prn = g[1:]
            if prn.lstrip("0") in have:
                continue
            el = GHOST_ELEV[i % len(GHOST_ELEV)]
            snr = 30 + 17 * math.sin(math.radians(el)) + (int(prn) * 7) % 5 - 2 + (self._n_groups + i) % 3 - 1
            sats.append([prn, f"{el:02d}", f"{(45 + 90 * i) % 360:03d}", f"{round(snr):02d}"])
        total = max(1, math.ceil(len(sats) / 4))
        out = []
        for k in range(total):
            chunk = [x for s in sats[4 * k:4 * k + 4] for x in s]
            body = ",".join(["GPGSV", str(total), str(k + 1), f"{len(sats):02d}", *chunk] + ([sig] if sig else []))
            out.append(f"${body}*{_checksum(body)}")
        return out

    def apply(self, line: str) -> str:
        """jump / drift: the line with the position (and speed) moved; every other line unchanged."""
        a = self.active
        if a is None or a["type"] == "sky" or not line.startswith("$") or "*" not in line:
            return line
        with self._lock:
            if self.active is None or self.active["type"] == "sky":
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
