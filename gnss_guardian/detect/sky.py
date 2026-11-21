"""Sky-geometry consistency (spoofing indicator): where should the GPS satellites be right now?

A GPS YUMA almanac (USCG NAVCEN, downloaded beforehand; read from disk, no network) gives each PRN's
orbit. From it the satellites' azimuth and elevation are predicted for the current time at the last
trusted position, and compared with what the receiver reports in GSV:

  sky_geom        median angle between the reported and the predicted direction of the listed GPS
                  satellites (a sky from another time or place, a stale or invented constellation)
  sky_impossible  number of tracked GPS satellites that should be below the horizon here and now

Prediction point and time are chosen so the attacker cannot steer them:
  * position: the last fix the detectors called clean (the rig cannot move far), not the reported one;
  * time: the host clock plus the baseline GNSS-minus-host offset (a time spoof does not move it).
    With no host clock (plain NMEA files) the reported GNSS time is used.

The check is off, with a reason, when the file is missing or unreadable, or its almanac reference
time is more than ``max_age_days`` from the epoch (judged on GNSS time: an offline Pi has no RTC).
GPS only (talker GP, PRN 1-32). Limit: a spoofer that synthesises the sky from a current ephemeris
(e.g. a software-defined spoofer fed today's broadcast ephemeris) produces a consistent sky; this
check catches replayed, time-shifted, remote-location, stale or hand-made skies.

Orbit model: IS-GPS-200 almanac equations (Kepler orbit plus node regression), WGS-84.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MU = 3.986005e14                 # m^3/s^2 (IS-GPS-200)
OMEGA_E = 7.2921151467e-5        # rad/s
WEEK_S = 604800
GPS_EPOCH = datetime(1980, 1, 6, tzinfo=timezone.utc)
WGS84_A, WGS84_F = 6378137.0, 1 / 298.257223563
_FIELDS = {"ID": "prn", "Health": "health", "Eccentricity": "e", "Time of Applicability(s)": "toa",
           "Orbital Inclination(rad)": "i0", "Rate of Right Ascen(r/s)": "omega_dot", "SQRT(A)  (m 1/2)": "sqrt_a",
           "Right Ascen at Week(rad)": "omega0", "Argument of Perigee(rad)": "w", "Mean Anom(rad)": "m0",
           "week": "week"}


@dataclass
class AlmanacEntry:
    prn: int
    health: int
    e: float
    toa: float
    i0: float
    omega_dot: float
    sqrt_a: float
    omega0: float
    w: float
    m0: float
    week: int                    # 10-bit week as broadcast (mod 1024)


def parse_yuma(path: str | Path) -> dict[int, AlmanacEntry]:
    """{PRN: entry} from a YUMA almanac file. Raises ValueError when nothing usable is in it."""
    out, cur = {}, {}

    def close():
        if cur and set(_FIELDS.values()) <= set(cur):
            out[int(cur["prn"])] = AlmanacEntry(prn=int(cur["prn"]), health=int(cur["health"]), e=cur["e"],
                                                toa=cur["toa"], i0=cur["i0"], omega_dot=cur["omega_dot"],
                                                sqrt_a=cur["sqrt_a"], omega0=cur["omega0"], w=cur["w"], m0=cur["m0"],
                                                week=int(cur["week"]))
    for line in Path(path).read_text(encoding="ascii", errors="replace").splitlines():
        if line.startswith("****"):
            close()
            cur = {}
            continue
        key, sep, val = line.partition(":")
        name = _FIELDS.get(key.strip()) if sep else None
        if name is not None:
            try:
                cur[name] = float(val.strip())
            except ValueError:
                pass
    close()
    if not out:
        raise ValueError(f"{path}: no YUMA almanac entries")
    return out


def gps_time(t: datetime | float, leap_s: float) -> tuple[int, float]:
    """(full GPS week, seconds of week) of a UTC time (datetime or POSIX seconds)."""
    ts = t.timestamp() if isinstance(t, datetime) else float(t)
    s = ts - GPS_EPOCH.timestamp() + leap_s
    week = int(s // WEEK_S)
    return week, s - week * WEEK_S


def full_week(week10: int, near_week: int) -> int:
    """Resolve a 10-bit almanac week to the full week nearest to ``near_week``."""
    k = round((near_week - week10) / 1024)
    return week10 + 1024 * k


def sat_ecef(a: AlmanacEntry, week: int, tow: float) -> tuple[float, float, float]:
    """Satellite position (ECEF, m) at GPS week/tow from its almanac entry (IS-GPS-200 table 20-IV)."""
    A = a.sqrt_a ** 2
    n0 = math.sqrt(MU / A ** 3)
    tk = (week - full_week(a.week, week)) * WEEK_S + tow - a.toa
    M = a.m0 + n0 * tk
    E = M
    for _ in range(12):
        E = M + a.e * math.sin(E)
    v = math.atan2(math.sqrt(1 - a.e ** 2) * math.sin(E), math.cos(E) - a.e)
    phi = v + a.w
    r = A * (1 - a.e * math.cos(E))
    x1, y1 = r * math.cos(phi), r * math.sin(phi)
    om = a.omega0 + (a.omega_dot - OMEGA_E) * tk - OMEGA_E * a.toa
    ci, si = math.cos(a.i0), math.sin(a.i0)
    return x1 * math.cos(om) - y1 * ci * math.sin(om), x1 * math.sin(om) + y1 * ci * math.cos(om), y1 * si


def geodetic_ecef(lat: float, lon: float, alt: float) -> tuple[float, float, float]:
    p, l = math.radians(lat), math.radians(lon)
    e2 = WGS84_F * (2 - WGS84_F)
    n = WGS84_A / math.sqrt(1 - e2 * math.sin(p) ** 2)
    return ((n + alt) * math.cos(p) * math.cos(l), (n + alt) * math.cos(p) * math.sin(l),
            (n * (1 - e2) + alt) * math.sin(p))


def az_el(lat: float, lon: float, alt: float, sat: tuple[float, float, float]) -> tuple[float, float]:
    """(elevation, azimuth) in degrees of an ECEF point seen from lat/lon/alt."""
    rx = geodetic_ecef(lat, lon, alt)
    dx, dy, dz = (s - r for s, r in zip(sat, rx))
    p, l = math.radians(lat), math.radians(lon)
    e = -math.sin(l) * dx + math.cos(l) * dy
    n = -math.sin(p) * math.cos(l) * dx - math.sin(p) * math.sin(l) * dy + math.cos(p) * dz
    u = math.cos(p) * math.cos(l) * dx + math.cos(p) * math.sin(l) * dy + math.sin(p) * dz
    return math.degrees(math.atan2(u, math.hypot(e, n))), math.degrees(math.atan2(e, n)) % 360.0


def predict(alm: dict[int, AlmanacEntry], t: datetime | float, lat: float, lon: float, alt: float,
            leap_s: float = 18.0, healthy_only: bool = True) -> dict[int, tuple[float, float]]:
    """{PRN: (elevation, azimuth)} of every (healthy) almanac satellite, horizon or not."""
    week, tow = gps_time(t, leap_s)
    return {prn: az_el(lat, lon, alt, sat_ecef(a, week, tow)) for prn, a in alm.items()
            if not (healthy_only and a.health != 0)}


def angle_deg(el1: float, az1: float, el2: float, az2: float) -> float:
    """Angle between two sky directions."""
    e1, a1, e2, a2 = map(math.radians, (el1, az1, el2, az2))
    c = math.sin(e1) * math.sin(e2) + math.cos(e1) * math.cos(e2) * math.cos(a1 - a2)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


class SkyCheck:
    """Loads the almanac once and compares each epoch's GPS GSV with the predicted sky.

    ``status`` is {"enabled": bool, "reason": str}; it changes (and the pipeline logs a sky_check
    event) when the almanac turns out to be too old for the data's GNSS time."""

    def __init__(self, cfg: dict, root: Path | None = None):
        c = cfg["detect"]["sky"]
        self.c = c
        self.alm: dict[int, AlmanacEntry] | None = None
        self.ref_time: float | None = None           # POSIX s of the almanac reference (week, toa)
        path = c.get("almanac_file")
        if not c.get("enabled", True):
            self.status = {"enabled": False, "reason": "sky check disabled in config (detect.sky.enabled)"}
            return
        if not path:
            self.status = {"enabled": False, "reason": "no almanac file configured (detect.sky.almanac_file)"}
            return
        p = Path(path)
        if not p.is_absolute() and root is not None:
            p = root / p
        if not p.is_file():
            self.status = {"enabled": False, "reason": f"almanac file {path} not found: download a GPS YUMA "
                                                       f"almanac (see README) to enable the sky check"}
            return
        try:
            self.alm = parse_yuma(p)
        except (OSError, ValueError) as e:
            self.status = {"enabled": False, "reason": f"almanac file {path} unreadable: {e}"}
            return
        self.path = str(path)
        a = next(iter(self.alm.values()))
        self._week10, self._toa = a.week, a.toa
        self.status = {"enabled": True, "reason": f"almanac {path}: {len(self.alm)} GPS satellites, week {a.week} "
                                                  f"(mod 1024)", "checked_age": False}

    @property
    def enabled(self) -> bool:
        return self.status["enabled"]

    def _check_age(self, t: float) -> None:
        """Once, on the first epoch with a time: is the almanac within max_age_days of the data?"""
        week, _ = gps_time(t, self.c["leap_s"])
        fw = full_week(self._week10, week)
        self.ref_time = GPS_EPOCH.timestamp() + fw * WEEK_S + self._toa - self.c["leap_s"]
        age_d = abs(t - self.ref_time) / 86400
        self.status["checked_age"] = True
        self.status["age_days"] = round(age_d, 1)
        if age_d > self.c["max_age_days"]:
            self.status.update(enabled=False, reason=f"almanac {self.path} is {age_d:.0f} days from the data's GNSS "
                                                     f"time (max {self.c['max_age_days']}): sky check off; download "
                                                     f"a current almanac")
            self.alm = None
        else:
            self.status["reason"] += f", {age_d:.1f} days from the data"

    def features(self, sats: dict, t: float | None, ref: tuple[float, float, float] | None) -> dict:
        """sky_* features for one epoch; ``sats`` the epoch's satellites, ``t`` POSIX UTC seconds,
        ``ref`` (lat, lon, alt) of the last clean fix."""
        f = {"sky_err_deg": None, "sky_n": 0, "sky_impossible": None, "sky_n_tracked": 0, "sky_impossible_prns": []}
        if self.alm is None or t is None:
            return f
        if not self.status.get("checked_age"):
            self._check_age(t)
            if self.alm is None:
                return f
        if ref is None:
            return f
        c = self.c
        pred = predict(self.alm, t, *ref, leap_s=c["leap_s"])
        errs, impossible, n_tracked = [], [], 0
        for k, s in sats.items():
            if not k.startswith("G"):
                continue
            prn = int(k[1:])
            if prn not in pred:                       # not in the almanac (new launch, unhealthy): no opinion
                continue
            pel, paz = pred[prn]
            if math.isfinite(s.cn0):
                n_tracked += 1
                if pel < c["below_deg"]:
                    impossible.append(k)
            if math.isfinite(s.elev) and math.isfinite(s.az) and s.elev >= c["mask_deg"]:
                errs.append(angle_deg(s.elev, s.az, pel, paz))
        if len(errs) >= c["min_sats"]:
            f["sky_err_deg"] = statistics.median(errs)
        f["sky_n"] = len(errs)
        f["sky_n_tracked"] = n_tracked
        if n_tracked >= c["min_sats"]:
            f["sky_impossible"] = len(impossible)
        f["sky_impossible_prns"] = sorted(impossible)
        return f
