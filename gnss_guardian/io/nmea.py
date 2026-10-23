"""Quectel L86 (MediaTek MT3333) NMEA 0183 parser: satellites (GSV), fix (RMC/GGA/GSA/VTG) and
antenna status (GPTXT ANTSTATUS, PGTOP,11).

Reused from IonWatch (checksum, PRN normalisation, multi-sentence GSV grouping, GSA used-in-fix,
host-prefix formats, PMTK001 acks) and extended with the fix fields gnss-guardian needs.

Capture formats (detected per line):
  plain         $GPGSV,...*hh
  iso_prefix    2026-10-20T13:30:00.123Z,$GPGSV,...*hh   (comma or space separated; IonWatch RawNmeaLog,
                                                         tools/record_nmea.py, fake_l86 --write)
  epoch_prefix  1792500000.123 $GPGSV,...*hh

The default L86-M33 output is GPS + GLONASS (GP/GL talkers, GN for combined RMC/GGA once
GLONASS is on); Galileo (GA) appears only after PMTK353.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta, timezone

ISO_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?)Z?[,\s]+(\$.*)$")
EPOCH_PREFIX = re.compile(r"^(\d{9,11}(?:\.\d+)?)[,\s]+(\$.*)$")
NMEA = re.compile(r"^\$([A-Z0-9]{2,})(,.*)?\*([0-9A-Fa-f]{2})$")
KNOT = 0.514444
PGTOP_ANTENNA = {"1": "SHORT", "2": "INTERNAL", "3": "OK"}    # MTK: 1 active antenna shorted, 2 internal, 3 active


def checksum_ok(sentence: str) -> bool:
    m = NMEA.match(sentence)
    if not m:
        return False
    body = sentence[1:sentence.rindex("*")]
    x = 0
    for ch in body:
        x ^= ord(ch)
    return x == int(m.group(3), 16)


def split_prefix(raw_line: str) -> tuple[str | None, datetime | None, str]:
    """(format, host time, sentence) of one captured line; format None for noise."""
    line = raw_line.strip()
    if line.startswith("$"):
        return "plain", None, line
    if (m := ISO_PREFIX.match(line)):
        t = datetime.fromisoformat(m.group(1).replace(" ", "T")).replace(tzinfo=timezone.utc)
        return "iso_prefix", t, m.group(2)
    if (m := EPOCH_PREFIX.match(line)):
        return "epoch_prefix", datetime.fromtimestamp(float(m.group(1)), tz=timezone.utc), m.group(2)
    return None, None, line


def sentence_type(sentence: str) -> str:
    """'RMC', 'GSV', 'PMTK001', ... from '$GPRMC,...'; '' if not NMEA-shaped."""
    if not sentence.startswith("$") or len(sentence) < 6:
        return ""
    addr = sentence[1:].split(",", 1)[0].split("*", 1)[0]
    return addr if addr.startswith("P") else addr[2:]


def normalise_prn(talker: str, prn: int, talkers: dict) -> tuple[str | None, int, str | None]:
    """(constellation, svid, note) from talker + raw PRN (MediaTek/Quectel conventions)."""
    const = None if talker == "GN" else talkers.get(talker)   # GN: decide by PRN range
    if const == "GPS" or talker == "GN":
        if 1 <= prn <= 32:
            return "GPS", prn, None
        if 33 <= prn <= 64 or 120 <= prn <= 158:
            return "SBAS", prn, None
        if 193 <= prn <= 202:
            return "QZSS", prn, None
        if 65 <= prn <= 96 and talker == "GN":
            return "GLONASS", prn - 64, None
        if 301 <= prn <= 336 and talker == "GN":
            return "Galileo", prn - 300, None
        if 401 <= prn <= 463 and talker == "GN":
            return "BeiDou", prn - 400, None
        return const or "GPS", prn, f"{talker} PRN {prn} outside known ranges"
    if const == "GLONASS":
        if 65 <= prn <= 96:
            return const, prn - 64, None
        return const, prn, (None if 1 <= prn <= 24 else f"GLONASS PRN {prn} kept raw")
    if const == "Galileo":
        if 1 <= prn <= 36:
            return const, prn, None
        if 301 <= prn <= 336:
            return const, prn - 300, None
        return const, prn, f"Galileo PRN {prn} not in 1-36 / 301-336: kept raw"
    if const == "BeiDou":
        return const, (prn - 200 if 201 <= prn <= 263 else prn), None
    if const == "QZSS":
        return const, prn + 192 if 1 <= prn <= 10 else prn, None
    return const, prn, (None if const else f"unknown talker {talker}")


def _f(x: str) -> float:
    try:
        return float(x)
    except ValueError:
        return math.nan


def _i(x: str) -> int | None:
    try:
        return int(x)
    except ValueError:
        return None


def _hms(s: str) -> timedelta | None:
    if len(s) < 6:
        return None
    try:
        return timedelta(hours=int(s[0:2]), minutes=int(s[2:4]), seconds=float(s[4:]))
    except ValueError:
        return None


def _deg(value: str, hemi: str) -> float | None:
    """NMEA ddmm.mmmm / dddmm.mmmm + hemisphere -> signed degrees."""
    if not value or not hemi or "." not in value:
        return None
    try:
        dot = value.index(".")
        deg = float(value[:dot - 2]) + float(value[dot - 2:]) / 60.0
    except ValueError:
        return None
    return -deg if hemi in ("S", "W") else deg


class NmeaParser:
    """Incremental NMEA parser: feed() one line at a time.

    Returns the satellite rows of every GSV group the line completes. The fix fields of the
    current update cycle (from RMC onwards: MTK order RMC, VTG, GGA, GSA, GSV, GLL) are kept in
    ``fix``; an RMC starts a new cycle. used_in_fix comes from the GSA sentences of the cycle.
    """

    def __init__(self, cfg: dict):
        self.talkers = cfg["l86"]["talkers"]
        self.counts = {"lines": 0, "nmea": 0, "bad_checksum": 0, "pmtk": 0, "noise": 0, "other_sentences": 0,
                       "gsv_groups": 0, "gsv_incomplete": 0, "rmc_no_fix": 0}
        self.notes: dict[str, int] = {}
        self.cur_date: date | None = None
        self.cur_time: datetime | None = None      # UTC of the latest RMC/GGA/ZDA
        self.fix: dict = {}
        self.antenna: str | None = None
        self.acks: list[tuple[str, str]] = []
        self.gsv_talkers: set[str] = set()         # every GSV talker seen (PMTK353 check)
        self._last_tod: timedelta | None = None
        self._groups: dict[tuple, dict] = {}
        self._used: set[tuple] = set()

    def feed(self, raw_line: str, host: datetime | None = None) -> list[dict]:
        """Parse one line; ``host`` is the receive time when the line has no host prefix."""
        self.counts["lines"] += 1
        if not raw_line.strip():
            return []
        fmt, h, sentence = split_prefix(raw_line)
        if fmt is None:
            self.counts["noise"] += 1
            return []
        host = h or host
        if not checksum_ok(sentence):
            self.counts["bad_checksum" if (NMEA.match(sentence) or "*" in sentence) else "noise"] += 1
            return []
        self.counts["nmea"] += 1
        fields = sentence[1:sentence.rindex("*")].split(",")
        addr = fields[0]
        if addr.startswith("PMTK"):
            self.counts["pmtk"] += 1
            if addr == "PMTK001" and len(fields) > 2:      # ack: command, flag (3 = success)
                self.acks.append((fields[1], fields[2]))
            return []
        if addr == "PGTOP":
            if len(fields) > 2 and fields[1] == "11":
                self.antenna = PGTOP_ANTENNA.get(fields[2], fields[2])
            return []
        talker, stype = addr[:2], addr[2:]
        if stype == "TXT":
            text = fields[4] if len(fields) > 4 else ""
            if text.startswith("ANTSTATUS="):
                self.antenna = text.split("=", 1)[1].strip()
            return []
        if stype in ("RMC", "GGA", "ZDA"):
            self._time_sentence(stype, fields, host)
            return []
        if stype == "GSA":
            self._gsa(talker, fields)
            return []
        if stype == "VTG":
            if len(fields) > 5:
                self.fix.setdefault("cog_deg", _f(fields[1]) if fields[1] else None)
                self.fix.setdefault("sog_mps", _f(fields[5]) * KNOT if fields[5] else None)
            return []
        if stype != "GSV":
            self.counts["other_sentences"] += 1
            return []
        return self._gsv(talker, fields, host)

    def _time_sentence(self, stype, fields, host):
        tod = _hms(fields[1]) if len(fields) > 1 else None
        if stype == "RMC":
            self.fix = {"talker": fields[0][:2]}
            self._used = set()
            valid = len(fields) > 2 and fields[2] == "A"
            self.fix["rmc_valid"] = valid
            if not valid:
                self.counts["rmc_no_fix"] += 1
            if len(fields) > 8:
                lat, lon = _deg(fields[3], fields[4]), _deg(fields[5], fields[6])
                if valid and lat is not None and lon is not None:
                    self.fix["lat"], self.fix["lon"] = lat, lon
                self.fix["sog_mps"] = _f(fields[7]) * KNOT if fields[7] else None
                self.fix["cog_deg"] = _f(fields[8]) if fields[8] else None
        elif stype == "GGA" and len(fields) > 9:
            q = _i(fields[6])
            self.fix["quality"] = q
            self.fix["sats_used"] = _i(fields[7])
            self.fix["hdop"] = _f(fields[8]) if fields[8] else None
            if q:
                self.fix["alt_m"] = _f(fields[9]) if fields[9] else None
                lat, lon = _deg(fields[2], fields[3]), _deg(fields[4], fields[5])
                if lat is not None and lon is not None:
                    self.fix.setdefault("lat", lat)
                    self.fix.setdefault("lon", lon)
        if stype == "RMC" and len(fields) > 9 and len(fields[9]) == 6:
            d = fields[9]
            try:
                self.cur_date = date(2000 + int(d[4:6]), int(d[2:4]), int(d[0:2]))
            except ValueError:
                pass
        elif stype == "ZDA" and len(fields) > 4 and fields[4]:
            self.cur_date = date(int(fields[4]), int(fields[3]), int(fields[2]))
        elif self.cur_date is None and host is not None:
            self.cur_date = host.date()
        elif (stype == "GGA" and self.cur_date is not None and tod is not None and self._last_tod is not None
              and tod < self._last_tod - timedelta(hours=12)):
            self.cur_date = self.cur_date + timedelta(days=1)   # midnight rollover seen only by GGA
        if tod is not None and self.cur_date is not None:
            self.cur_time = datetime.combine(self.cur_date, datetime.min.time(), tzinfo=timezone.utc) + tod
            self._last_tod = tod
            if stype == "RMC":
                self.fix["time"] = self.cur_time

    def _gsa(self, talker, fields):
        if len(fields) < 18:
            return
        sys_id = fields[18] if len(fields) > 18 and fields[18] else None
        tk = {"1": "GP", "2": "GL", "3": "GA", "4": "GB", "5": "GQ", "6": "GI"}.get(sys_id, talker)
        self.fix["fix_mode"] = _i(fields[2])
        for k, i in (("pdop", 15), ("hdop_gsa", 16), ("vdop", 17)):
            if fields[i]:
                self.fix[k] = _f(fields[i])
        for p in fields[3:15]:
            if p.strip().isdigit():
                c, sv, _ = normalise_prn(tk, int(p), self.talkers)
                self._used.add((c, sv))

    def _flush(self, key, complete=True) -> list[dict]:
        g = self._groups.pop(key, None)
        if not g:
            return []
        self.counts["gsv_groups"] += 1
        if not complete:
            self.counts["gsv_incomplete"] += 1
        for r in g["sats"]:
            r["used_in_fix"] = (r["constellation"], r["svid"]) in self._used
        return g["sats"]

    def finish(self) -> list[dict]:
        """Flush GSV groups still open (end of file / stream)."""
        out = []
        for key in list(self._groups):
            out += self._flush(key, complete=False)
        return out

    def _gsv(self, talker, fields, host) -> list[dict]:
        out = []
        try:
            total, num = int(fields[1]), int(fields[2])
        except (ValueError, IndexError):
            self.counts["other_sentences"] += 1
            return out
        self.gsv_talkers.add(talker)
        body = fields[4:]
        sig_id = body.pop() if len(body) % 4 == 1 else ""
        key = (talker, sig_id)
        if num == 1:
            if key in self._groups:
                out += self._flush(key, complete=False)
            self._groups[key] = {"time": self.cur_time or host, "sats": []}
        g = self._groups.get(key)
        if g is None:            # continuation without its first sentence
            self.counts["gsv_incomplete"] += 1
            return out
        for i in range(0, len(body) - 3, 4):
            prn_s = body[i].strip()
            if not prn_s.isdigit():
                continue
            prn = int(prn_s)
            const, svid, note = normalise_prn(talker, prn, self.talkers)
            if note:
                self.notes[note] = self.notes.get(note, 0) + 1
            cn0 = _f(body[i + 3]) if body[i + 3] else math.nan
            g["sats"].append({"time": g["time"], "talker": talker, "constellation": const, "prn_raw": prn,
                              "svid": svid, "elev_deg": _f(body[i + 1]) if body[i + 1] else math.nan,
                              "az_deg": _f(body[i + 2]) if body[i + 2] else math.nan,
                              "cn0_dbhz": cn0 if cn0 > 0 else math.nan})
        if num == total:
            out += self._flush(key)
        return out
