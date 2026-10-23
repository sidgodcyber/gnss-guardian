"""Group NMEA lines into one Epoch per receiver update cycle (every fix: 1, 5 or 10 Hz).

A cycle starts at an RMC (MTK order RMC, VTG, GGA, GSA, GSV, GLL). The previous cycle is emitted
when the next RMC arrives or when the caller calls flush() because the burst is over (live
mode: an empty serial read), so the detector sees each fix with at most one read timeout of
delay. Satellites come from the latest complete GSV group per talker; at 5/10 Hz the L86 may
send GSV less often than RMC, so an epoch carries the snapshot's age and ``sats_new`` says
whether a fresh C/N0 sample arrived in this cycle.

Time bases:
  t_gnss  UTC from the receiver (RMC time + date): what a spoofer controls
  t_host  host UTC when the cycle's RMC was received (prefix of a recorded line, or live clock)
  mono    host monotonic seconds (live), or the host prefix as seconds (replay of recorded NMEA);
          None for plain NMEA without host time (the time-vs-clock check is then disabled)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import NamedTuple

from gnss_guardian.io.nmea import NmeaParser, checksum_ok, sentence_type, split_prefix

LETTER = {"GPS": "G", "SBAS": "S", "GLONASS": "R", "QZSS": "J", "BeiDou": "C", "Galileo": "E", "NavIC": "I"}
SNAPSHOT_MAX_AGE_S = 3.0


class Sat(NamedTuple):
    cn0: float          # dB-Hz, nan when the receiver lists the satellite without C/N0 (not tracked)
    elev: float
    az: float
    used: bool


@dataclass
class Epoch:
    seq: int
    t_gnss: datetime | None
    t_host: datetime | None
    mono: float | None
    fix_valid: bool
    quality: int | None = None
    fix_mode: int | None = None
    lat: float | None = None
    lon: float | None = None
    alt_m: float | None = None
    sog_mps: float | None = None
    cog_deg: float | None = None
    hdop: float | None = None
    pdop: float | None = None
    vdop: float | None = None
    sats_used: int | None = None
    sats: dict[str, Sat] = field(default_factory=dict)
    sats_new: bool = False
    sats_age_s: float | None = None
    antenna: str | None = None
    talkers: frozenset = frozenset()

    def tracked(self) -> dict[str, Sat]:
        """Satellites with a C/N0 value (SBAS excluded)."""
        return {k: s for k, s in self.sats.items() if not k.startswith("S") and math.isfinite(s.cn0)}


def sat_key(constellation: str | None, svid: int) -> str:
    return f"{LETTER.get(constellation or '', '?')}{svid:02d}"


class EpochBuilder:
    def __init__(self, cfg: dict):
        self.parser = NmeaParser(cfg)
        self.seq = 0
        self._cur: dict | None = None
        self._snap: dict[str, tuple[float | None, list[dict]]] = {}     # talker -> (mono, rows)
        self._new_gsv = False

    def feed(self, raw_line: str, host: datetime | None = None, mono: float | None = None) -> list[Epoch]:
        """One line in, the epochs it completes out. ``host``/``mono`` describe the receive time
        when the line has no host prefix (live mode)."""
        fmt, h, sentence = split_prefix(raw_line)
        kind = sentence_type(sentence) if fmt else ""
        out = []
        is_rmc = kind == "RMC" and checksum_ok(sentence)
        if is_rmc and self._cur is not None:
            out += self.flush()
        host = h or host
        if mono is None and h is not None:
            mono = h.timestamp()
        n_groups = self.parser.counts["gsv_groups"]
        rows = self.parser.feed(sentence if fmt else raw_line, host)
        if is_rmc:
            self._cur = {"host": host, "mono": mono}
        if kind == "GSV" and self.parser.counts["gsv_groups"] > n_groups:
            talker = sentence[1:3]
            self._snap[talker] = (mono, [r for r in rows if r["talker"] == talker])
            self._new_gsv = True
        return out

    def flush(self) -> list[Epoch]:
        """Emit the open cycle (end of an NMEA burst, or end of file)."""
        if self._cur is None:
            return []
        cur, self._cur = self._cur, None
        f = self.parser.fix
        mono = cur["mono"]
        sats, ages = {}, []
        for talker, (m, rows) in list(self._snap.items()):
            age = (mono - m) if (mono is not None and m is not None) else 0.0
            if age > SNAPSHOT_MAX_AGE_S:
                del self._snap[talker]              # talker went silent: its satellites are gone
                continue
            ages.append(age)
            for r in rows:
                sats[sat_key(r["constellation"], r["svid"])] = Sat(r["cn0_dbhz"], r["elev_deg"], r["az_deg"],
                                                                   bool(r.get("used_in_fix")))
        quality = f.get("quality")
        valid = bool(f.get("rmc_valid")) and (quality is None or quality > 0) and f.get("lat") is not None
        hdop = f.get("hdop") if f.get("hdop") is not None else f.get("hdop_gsa")
        ep = Epoch(seq=self.seq, t_gnss=f.get("time"), t_host=cur["host"], mono=mono, fix_valid=valid,
                   quality=quality, fix_mode=f.get("fix_mode"),
                   lat=f.get("lat") if valid else None, lon=f.get("lon") if valid else None,
                   alt_m=f.get("alt_m") if valid else None,
                   sog_mps=f.get("sog_mps") if valid else None, cog_deg=f.get("cog_deg") if valid else None,
                   hdop=hdop if valid else None, pdop=f.get("pdop"), vdop=f.get("vdop"),
                   sats_used=f.get("sats_used"), sats=sats, sats_new=self._new_gsv,
                   sats_age_s=max(ages) if ages else None, antenna=self.parser.antenna,
                   talkers=frozenset(self.parser.gsv_talkers))
        self.seq += 1
        self._new_gsv = False
        return [ep]


def epochs_from_lines(lines, cfg: dict) -> list[Epoch]:
    b = EpochBuilder(cfg)
    out = []
    for line in lines:
        out += b.feed(line)
    out += b.flush()
    return out
