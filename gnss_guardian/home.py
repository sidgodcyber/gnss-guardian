"""Home point: given on the command line (--home lat,lon) or set automatically as the median of the
first ``home.set_after_s`` seconds of trusted fixes after warm-up. Stored in session.json; vision
navigation and return-home (Phases 2-3) use it as the origin of the local east/north grid."""
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
    def __init__(self, cfg: dict, given: tuple[float, float] | None = None):
        self.set_after_s = cfg["home"]["set_after_s"]
        self.home: dict | None = None
        if given is not None:
            self.home = {"lat": given[0], "lon": given[1], "alt_m": None, "source": "command line",
                         "n_fixes": 0, "set_at_utc": None}
        self._fixes: list[tuple[float, float, float, float | None]] = []

    def update(self, v: dict, t: float) -> dict | None:
        """Feed a verdict at time t (s); returns the home dict when it has just been set."""
        if self.home is not None or not v["gnss_trusted"]:
            return None
        self._fixes.append((t, v["lat"], v["lon"], v["alt_m"]))
        if t - self._fixes[0][0] < self.set_after_s:
            return None
        alts = [a for *_, a in self._fixes if a is not None]
        self.home = {"lat": round(statistics.median(f[1] for f in self._fixes), 7),
                     "lon": round(statistics.median(f[2] for f in self._fixes), 7),
                     "alt_m": round(statistics.median(alts), 1) if alts else None,
                     "source": f"median of the first {self.set_after_s} s of trusted fixes",
                     "n_fixes": len(self._fixes), "set_at_utc": v["t_gnss"]}
        return self.home

    def enu(self, lat: float | None, lon: float | None) -> tuple[float, float] | None:
        if self.home is None or lat is None or lon is None:
            return None
        return geo.enu(lat, lon, self.home["lat"], self.home["lon"])
