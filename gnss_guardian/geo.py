"""Small-area geometry: local east/north metres about a reference point (equirectangular; fine
for the few kilometres a field test covers) and great-circle distance."""
from __future__ import annotations

import math

R_EARTH = 6_371_008.8


def enu(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    """(east, north) metres of (lat, lon) from (lat0, lon0)."""
    e = math.radians(lon - lon0) * R_EARTH * math.cos(math.radians(lat0))
    n = math.radians(lat - lat0) * R_EARTH
    return e, n


def from_enu(e: float, n: float, lat0: float, lon0: float) -> tuple[float, float]:
    lat = lat0 + math.degrees(n / R_EARTH)
    lon = lon0 + math.degrees(e / (R_EARTH * math.cos(math.radians(lat0))))
    return lat, lon


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_EARTH * math.asin(min(1.0, math.sqrt(a)))


def velocity_en(sog_mps: float, cog_deg: float) -> tuple[float, float]:
    """(v_east, v_north) from speed over ground and course (degrees from true north)."""
    c = math.radians(cog_deg)
    return sog_mps * math.sin(c), sog_mps * math.cos(c)
