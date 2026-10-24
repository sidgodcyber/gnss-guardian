"""Home point: given on the command line (--home lat,lon) or set automatically as the median of
``home.set_after_s`` seconds of trusted fixes after warm-up that pass a quality gate (HDOP, satellites
used, spread). Stored in session.json; vision navigation and return-home (Phases 2-3) use it as the
origin of the local east/north grid."""
from __future__ import annotations

import statistics

from gnss_guardian import geo


def parse_home(text: str | None) -> tuple[float, float] | None:
    if not text:
        return None
    lat, lon = (float(x) for x in text.split(","))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError(f"home {text!r} is not lat,lon in degrees")
    return lat, lon


class HomeSetter:
    """Auto home: the newest ``set_after_s`` of consecutive trusted fixes, once the window passes the
    quality gate (median HDOP <= max_hdop, median satellites used >= min_sats_used, every fix within
    max_spread_m of the median). Until then ``waiting`` says why; an untrusted epoch restarts the window."""

    def __init__(self, cfg: dict, given: tuple[float, float] | None = None):
        h = cfg["home"]
        self.set_after_s = h["set_after_s"]
        self.max_hdop, self.min_sats, self.max_spread = h["max_hdop"], h["min_sats_used"], h["max_spread_m"]
        self.home: dict | None = None
        self.waiting: str | None = "waiting for trusted GNSS"
        if given is not None:
            self.home = {"lat": given[0], "lon": given[1], "alt_m": None, "source": "command line",
                         "n_fixes": 0, "set_at_utc": None}
            self.waiting = None
        self._fixes: list[tuple[float, float, float, float | None, float | None, int | None]] = []

    def update(self, v: dict, t: float) -> dict | None:
        """Feed a verdict at time t (s); returns the home dict when it has just been set."""
        if self.home is not None:
            return None
        if not v["gnss_trusted"]:
            if self._fixes:
                self.waiting = "window restarted: GNSS not trusted"
            self._fixes = []
            return None
        f = v.get("features") or {}
        self._fixes.append((t, v["lat"], v["lon"], v["alt_m"], v.get("hdop"), f.get("n_used")))
        while len(self._fixes) > 1 and t - self._fixes[1][0] >= self.set_after_s:
            self._fixes.pop(0)                      # keep the newest window that spans set_after_s
        span = t - self._fixes[0][0]
        if span < self.set_after_s:
            self.waiting = f"collecting trusted fixes: {span:.0f}/{self.set_after_s} s"
            return None
        lat0 = statistics.median(x[1] for x in self._fixes)
        lon0 = statistics.median(x[2] for x in self._fixes)
        spread = max(geo.distance_m(lat0, lon0, x[1], x[2]) for x in self._fixes)
        hdops = [x[4] for x in self._fixes if x[4] is not None]
        sats = [x[5] for x in self._fixes if x[5] is not None]
        hdop = statistics.median(hdops) if hdops else None
        n_sats = statistics.median(sats) if sats else None
        bad = []
        if hdop is None or hdop > self.max_hdop:
            bad.append(f"HDOP {'-' if hdop is None else f'{hdop:.1f}'} > {self.max_hdop:g}")
        if n_sats is None or n_sats < self.min_sats:
            bad.append(f"{'-' if n_sats is None else f'{n_sats:.0f}'} satellites used < {self.min_sats}")
        if spread > self.max_spread:
            bad.append(f"fixes spread {spread:.1f} m > {self.max_spread:g} m")
        if bad:
            self.waiting = f"home not set: {', '.join(bad)} over the last {self.set_after_s} s"
            return None
        alts = [x[3] for x in self._fixes if x[3] is not None]
        self.home = {"lat": round(lat0, 7), "lon": round(lon0, 7),
                     "alt_m": round(statistics.median(alts), 1) if alts else None,
                     "source": f"median of {self.set_after_s} s of trusted fixes",
                     "n_fixes": len(self._fixes), "spread_m": round(spread, 2),
                     "hdop": round(hdop, 2), "sats_used": n_sats, "set_at_utc": v["t_gnss"]}
        self.waiting = None
        return self.home

    def enu(self, lat: float | None, lon: float | None) -> tuple[float, float] | None:
        if self.home is None or lat is None or lon is None:
            return None
        return geo.enu(lat, lon, self.home["lat"], self.home["lon"])
