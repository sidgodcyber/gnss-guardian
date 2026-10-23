"""Fake Quectel L86: replays an NMEA fixture as a serial stream with jamming / spoofing profiles,
and obeys the PMTK commands gnss-guardian sends (baud, output rate, sentences, constellations).

    python tools/fake_l86.py [--fixture tests/fixtures/l86_sim_10min.nmea] [--transport auto|pty|tcp]
                             [--speed 1] [--start 2026-10-22T14:00:00Z] [--duration-s 600]
                             [--walk loop:W:H:MPS]
                             [--jam ramp|sudden --jam-at T [--jam-rate-db-s 4] [--jam-depth-db 25]]
                             [--spoof jump|drift|time --spoof-at T [--jump-m 500] [--drift-mps 1.0]
                              [--time-offset-s 30] [--spoof-cn0 uniform|natural]]
                             [--recover-after DUR]
                             [--write FILE [--write-rate HZ]]
                             [--inject-fade PRN:START:DUR:DEPTH] [--inject-dropout START:DUR]
                             [--inject-garbage T[,T...]] [--inject-disconnect START:DUR]

The first stdout line is ``PORT <path-or-url>``; give that to ``gnss-guardian live --l86``:
  * pty (Linux/macOS, the default there): a pseudo-terminal such as /dev/pts/5;
  * tcp (Windows, or --transport tcp): ``socket://127.0.0.1:<port>``, which pyserial opens like a
    serial port. Windows has no pty; this is the laptop substitute.
``--write FILE`` instead renders the whole run at once into FILE, every line prefixed with a host
receive time (``2026-10-22T14:00:00.412Z,$GPRMC,...``, the tools/record_nmea.py format), for replay.

Clock: one fixture cycle (RMC ... GSV) is one simulated second; ``--speed`` simulated seconds run per
wall second. T/START/DUR values are simulated seconds from --start.

Receiver model (simple, stated here so test results can be read against it):
  * a satellite is tracked while its C/N0 >= 12 dB-Hz and re-acquired once >= 20 dB-Hz again;
    lost satellites stay in GSV with an empty SNR, as the MTK lists them from the almanac;
  * the fix needs >= 4 satellites at >= 25 dB-Hz; after a loss it returns 3 s after that holds;
  * HDOP grows as sqrt(base satellites used / satellites used).
Profiles:
  --jam sudden     C/N0 of every satellite drops by --jam-depth-db at --jam-at
  --jam ramp       ... falling at --jam-rate-db-s from --jam-at
  --spoof jump     reported position jumps --jump-m (bearing 45 deg) at --spoof-at
  --spoof drift    reported position drags off at --drift-mps (bearing 60 deg), speed/course follow
  --spoof time     reported GNSS time runs --time-offset-s ahead
  every spoof      with --spoof-cn0 uniform (default) the receiver tracks the spoofer's sky: half of
                   each constellation's satellites swapped for PRNs not in view, all at 48 +/- 0.5 dB-Hz;
                   natural keeps the real satellites and C/N0 (kinematic/time signature only)
  --recover-after  the jam/spoof ends DUR s after it began (C/N0 climbs back at 10 dB/s)
L86 behaviour:
  PMTK251,<baud>  switch baud (acked at the old baud); the line carries at most baud/10 bytes/s
  PMTK220,<ms>    fix interval -> output rate 1000/ms Hz, capped at --max-rate
  PMTK314,...     sentence selection (GLL,RMC,VTG,GGA,GSA,GSV,...)
  PMTK353,a,b,c,d,e  GPS, GLONASS, Galileo, Galileo-full, BeiDou; Galileo (GAGSV) is off at power-on,
                  like the real GPS+GLONASS L86-M33. Settings are lost on --inject-disconnect.
"""
from __future__ import annotations

import argparse
import math
import os
import random
import select
import socket
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "l86_sim_10min.nmea"
PMTK314_FIELDS = ["GLL", "RMC", "VTG", "GGA", "GSA", "GSV"]
TRACK_FLOOR, ACQ_FLOOR, FIX_FLOOR, FIX_REACQ_S, RECOVER_DB_S = 12, 20, 25, 3.0, 10.0
SPOOF_CN0, SPOOF_CN0_SD = 48.0, 0.5
R_EARTH = 6_371_008.8
KNOT = 0.514444
NEW_PRNS = {"GP": [6, 7, 10, 13, 17, 22, 30, 31], "GL": [66, 69, 70, 77, 78, 83], "GA": [1, 4, 11, 19, 21, 26]}


def checksum(body: str) -> str:
    x = 0
    for ch in body:
        x ^= ord(ch)
    return f"{x:02X}"


def sentence(body: str) -> str:
    return f"${body}*{checksum(body)}"


def load_cycles(path: Path) -> list[list[str]]:
    cycles, cur = [], []
    for line in path.read_text(encoding="ascii").splitlines():
        line = line.strip()
        if "$" in line:
            line = line[line.index("$"):]
        if not line.startswith("$"):
            continue
        if line[3:6] == "RMC" and cur:
            cycles.append(cur)
            cur = []
        cur.append(line)
    if cur:
        cycles.append(cur)
    return cycles


def parse_cycle(cycle: list[str]) -> dict:
    """{'sats': {talker: [(prn, el, az, snr|None)]}, 'used': {talker: [prn]}, 'pdop', 'hdop', 'vdop',
    'lat', 'lon', 'alt'} from one fixture cycle."""
    out = {"sats": {}, "used": {}, "pdop": 1.6, "hdop": 0.9, "vdop": 1.3, "lat": None, "lon": None, "alt": 500.0}
    for s in cycle:
        f = s[1:s.rindex("*")].split(",")
        talker, kind = f[0][:2], f[0][2:]
        if kind == "GSV":
            body = f[4:]
            if len(body) % 4 == 1:
                body = body[:-1]
            for i in range(0, len(body) - 3, 4):
                if body[i].strip().isdigit():
                    out["sats"].setdefault(talker, []).append(
                        (int(body[i]), int(body[i + 1] or 0), int(body[i + 2] or 0),
                         int(body[i + 3]) if body[i + 3].strip() else None))
        elif kind == "GSA":
            out["used"][talker] = [int(p) for p in f[3:15] if p.strip().isdigit()]
            out["pdop"], out["hdop"], out["vdop"] = (float(f[15] or 1.6), float(f[16] or 0.9), float(f[17] or 1.3))
        elif kind == "GGA" and f[2]:
            out["lat"] = _deg(f[2], f[3])
            out["lon"] = _deg(f[4], f[5])
            out["alt"] = float(f[9] or 500.0)
    return out


def _deg(v: str, hemi: str) -> float:
    dot = v.index(".")
    d = float(v[:dot - 2]) + float(v[dot - 2:]) / 60
    return -d if hemi in "SW" else d


def _nmea_ll(lat: float, lon: float) -> tuple[str, str, str, str]:
    def fmt(x, width):
        a = abs(x)
        d = int(a)
        m = (a - d) * 60
        return f"{d:0{width}d}{m:07.4f}"
    return fmt(lat, 2), "N" if lat >= 0 else "S", fmt(lon, 3), "E" if lon >= 0 else "W"


def walk_at(spec: str | None, s: float) -> tuple[float, float, float, float]:
    """(east, north, v_east, v_north) of the rig at simulated second s. spec 'loop:W:H:MPS':
    a W x H m rectangle walked east, north, west, south from the start point."""
    if not spec:
        return 0.0, 0.0, 0.0, 0.0
    _, w, h, v = spec.split(":")
    w, h, v = float(w), float(h), float(v)
    d = (v * max(s, 0.0)) % (2 * (w + h))
    for length, (ue, un), (oe, on) in ((w, (1, 0), (0, 0)), (h, (0, 1), (w, 0)),
                                       (w, (-1, 0), (w, h)), (h, (0, -1), (0, h))):
        if d <= length:
            return oe + ue * d, on + un * d, ue * v, un * v
        d -= length
    return 0.0, 0.0, 0.0, 0.0


class Scenario:
    """Renders fixture cycles into NMEA under the walk / jam / spoof profiles. render() must be
    called in time order (it keeps per-satellite tracking state)."""

    def __init__(self, cycles: list[list[str]], a, start: datetime):
        self.cyc = [parse_cycle(c) for c in cycles]
        first = next(c for c in self.cyc if c["lat"] is not None)
        self.lat0, self.lon0, self.alt0 = first["lat"], first["lon"], first["alt"]
        self.a, self.start = a, start
        self.rng = random.Random(a.seed)
        self.lost: set = set()
        self.fix_ok_since: float | None = 0.0
        self.has_fix = True

    def _end(self, at):
        return at + self.a.recover_after if (at is not None and self.a.recover_after is not None) else None

    def jam_depth(self, s: float) -> float:
        a = self.a
        if not a.jam or s < a.jam_at:
            return 0.0
        def raw(x):
            return a.jam_depth_db if a.jam == "sudden" else min(a.jam_depth_db, a.jam_rate_db_s * (x - a.jam_at))
        end = self._end(a.jam_at)
        if end is None or s < end:
            return raw(s)
        return max(0.0, raw(end) - RECOVER_DB_S * (s - end))

    def spoofing(self, s: float) -> bool:
        a = self.a
        end = self._end(a.spoof_at)
        return bool(a.spoof) and s >= a.spoof_at and (end is None or s < end)

    def render(self, k: int, s: float, enabled: set, galileo: bool, fade=None) -> list[str]:
        a = self.a
        c = self.cyc[k % len(self.cyc)]
        e, n, ve, vn = walk_at(a.walk, s)
        t = self.start + timedelta(seconds=s)
        spoof = self.spoofing(s)
        if spoof and a.spoof == "jump":
            e += a.jump_m * math.sin(math.radians(45))
            n += a.jump_m * math.cos(math.radians(45))
        elif spoof and a.spoof == "drift":
            dd = a.drift_mps * (s - a.spoof_at)
            e += dd * math.sin(math.radians(60))
            n += dd * math.cos(math.radians(60))
            ve += a.drift_mps * math.sin(math.radians(60))
            vn += a.drift_mps * math.cos(math.radians(60))
        elif spoof and a.spoof == "time":
            t += timedelta(seconds=a.time_offset_s)
        depth = self.jam_depth(s)
        talkers = [tk for tk in ("GP", "GL", "GA") if tk in c["sats"] and (tk != "GA" or galileo)]
        sky: dict[str, list] = {}
        for tk in talkers:
            sats = []
            src = c["sats"][tk]
            if spoof and a.spoof_cn0 == "uniform":
                keep = src[: (len(src) + 1) // 2]
                extra = [(p, 20 + 9 * i % 60, (37 * p) % 360, None) for i, p in enumerate(NEW_PRNS[tk])]
                src = keep + extra[: len(src) - len(keep)]
            for prn, el, az, snr in src:
                key = (tk, prn)
                if spoof and a.spoof_cn0 == "uniform":
                    cn0 = round(self.rng.gauss(SPOOF_CN0, SPOOF_CN0_SD))
                elif snr is None:
                    cn0 = None
                else:
                    cn0 = snr - depth
                    if fade and prn == fade[0] and fade[1] <= s < fade[1] + fade[2]:
                        cn0 -= fade[3]
                if cn0 is not None:
                    if key in self.lost and cn0 >= ACQ_FLOOR:
                        self.lost.discard(key)
                    elif cn0 < TRACK_FLOOR:
                        self.lost.add(key)
                if key in self.lost:
                    cn0 = None
                sats.append((prn, el, az, None if cn0 is None else max(int(round(cn0)), 1)))
            sky[tk] = sats
        strong = {(tk, p) for tk, ss in sky.items() for p, _, _, c0 in ss if c0 is not None and c0 >= FIX_FLOOR}
        if len(strong) >= 4:
            self.fix_ok_since = s if self.fix_ok_since is None else self.fix_ok_since
            self.has_fix = self.has_fix or (s - self.fix_ok_since >= FIX_REACQ_S)
        else:
            self.fix_ok_since, self.has_fix = None, False
        if spoof and a.spoof_cn0 == "uniform":
            used = {tk: [p for p, *_ in sky[tk] if (tk, p) in strong] for tk in talkers}
        else:
            used = {tk: [p for p in c["used"].get(tk, []) if (tk, p) in strong] for tk in talkers}
        n_used = sum(len(v) for v in used.values()) if self.has_fix else 0
        n_base = max(sum(len(c["used"].get(tk, [])) for tk in talkers), 1)
        hdop = c["hdop"] * math.sqrt(n_base / max(n_used, 1))
        hms = f"{t:%H%M%S}.{t.microsecond // 1000:03d}"
        out = []
        if self.has_fix:
            lat = self.lat0 + math.degrees(n / R_EARTH)
            lon = self.lon0 + math.degrees(e / (R_EARTH * math.cos(math.radians(self.lat0))))
            la, ns, lo, ew = _nmea_ll(lat, lon)
            sog = math.hypot(ve, vn) / KNOT
            cog = math.degrees(math.atan2(ve, vn)) % 360 if sog > 0.05 else 0.0
            if not a.walk and not (spoof and a.spoof == "drift"):
                sog = 0.02
            out.append(f"GPRMC,{hms},A,{la},{ns},{lo},{ew},{sog:.2f},{cog:.2f},{t:%d%m%y},,,A")
            out.append(f"GPVTG,{cog:.2f},T,,M,{sog:.2f},N,{sog * 1.852:.2f},K,A")
            out.append(f"GPGGA,{hms},{la},{ns},{lo},{ew},1,{n_used:02d},{hdop:.2f},{self.alt0:.1f},M,-40.0,M,,")
            for tk in talkers:
                prns = [f"{p:02d}" for p in used[tk]][:12]
                pd_, hd, vd = c["pdop"] * hdop / c["hdop"], hdop, c["vdop"] * hdop / c["hdop"]
                out.append(f"{tk}GSA,A,3,{','.join(prns + [''] * (12 - len(prns)))},{pd_:.2f},{hd:.2f},{vd:.2f}")
        else:
            out.append(f"GPRMC,{hms},V,,,,,0.00,0.00,{t:%d%m%y},,,N")
            out.append("GPVTG,0.00,T,,M,0.00,N,0.00,K,N")
            out.append(f"GPGGA,{hms},,,,,0,00,,,M,,M,,")
            out.append("GPGSA,A,1,,,,,,,,,,,,,,,")
        for tk in talkers:
            sats = sky[tk]
            chunks = [sats[i:i + 4] for i in range(0, len(sats), 4)] or [[]]
            for i, ch in enumerate(chunks, 1):
                body = f"{tk}GSV,{len(chunks)},{i},{len(sats):02d}"
                if ch:
                    body += "," + ",".join(f"{p:02d},{el:02d},{az:03d},{'' if c0 is None else f'{c0:02d}'}"
                                           for p, el, az, c0 in ch)
                out.append(body)
        return [sentence(b) for b in out if b[2:5] in enabled]


def garbage(t: datetime) -> list[bytes]:
    hms = f"{t:%H%M%S}.000"
    body = f"GPGGA,{hms},2915.336,N,07932.220,E,1,08,1.0,500.0,M,-40.0,M,,"
    return [b"\x00\xff\xfe\x13 noise \x81\x82\r\n",
            ("$GPGSV,3,1,11,02,62,040,4").encode(),           # cut in half, no newline
            f"${body}*00\r\n".encode()]                        # bad checksum


class Transport:
    path = ""

    def send(self, data: bytes) -> None: ...
    def connected(self) -> bool: ...
    def poll(self) -> bytes: ...
    def disconnect(self) -> None: ...
    def close(self) -> None: ...


class PtyTransport(Transport):
    def __init__(self):
        import tty
        self.master, slave = os.openpty()
        tty.setraw(slave)
        self.path = os.ttyname(slave)
        self._slave = slave
        os.set_blocking(self.master, False)

    def send(self, data):
        try:
            os.write(self.master, data)
        except BlockingIOError:      # nobody reading: drop, like a UART would
            pass

    def connected(self):
        return True

    def poll(self) -> bytes:
        got = b""
        try:
            while select.select([self.master], [], [], 0)[0]:
                got += os.read(self.master, 4096)
        except OSError:
            pass
        return got

    def disconnect(self):
        pass

    def close(self):
        os.close(self.master)
        os.close(self._slave)


class TcpTransport(Transport):
    def __init__(self, port: int):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", port))
        self.srv.listen(1)
        self.srv.setblocking(False)
        self.path = f"socket://127.0.0.1:{self.srv.getsockname()[1]}"
        self.cli = None
        self.refuse = False

    def poll(self) -> bytes:
        if self.cli is None:
            try:
                c, _ = self.srv.accept()
            except (BlockingIOError, OSError):
                return b""
            if self.refuse:
                c.close()
                return b""
            c.setblocking(False)
            self.cli = c
        got = b""
        try:
            while True:
                b = self.cli.recv(4096)
                if not b:                # reader closed the port (e.g. reopening at a new baud)
                    self.cli.close()
                    self.cli = None
                    break
                got += b
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            self.cli = None
        return got

    def connected(self):
        return self.cli is not None

    def send(self, data):
        if self.cli is None:
            return
        try:
            self.cli.setblocking(True)
            self.cli.sendall(data)
            self.cli.setblocking(False)
        except OSError:
            self.cli = None

    def disconnect(self):
        if self.cli is not None:
            try:
                self.cli.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.cli.close()
            self.cli = None

    def close(self):
        self.disconnect()
        self.srv.close()


class Module:
    """L86 state driven by PMTK commands."""

    def __init__(self, baud: int, max_rate: float):
        self.default_baud, self.max_rate = baud, max_rate
        self.reset()
        self._buf = b""

    def reset(self):
        self.baud, self.rate = self.default_baud, 1.0
        self.enabled = {"RMC", "GGA", "GSA", "GSV", "VTG", "GLL"}
        self.galileo = False
        self.banner = True                   # GPTXT ANTSTATUS after power-on

    def command(self, data: bytes) -> list[bytes]:
        """Handle incoming bytes; return acks to send."""
        acks = []
        self._buf += data
        *lines, self._buf = self._buf.split(b"\n")
        for raw in lines:
            line = raw.decode("ascii", "replace").strip()
            if not line.startswith("$PMTK") or "*" not in line:
                continue
            body = line[1:line.rindex("*")]
            if checksum(body) != line[line.rindex("*") + 1:].upper()[:2]:
                continue
            f = body.split(",")
            cmd = f[0][4:]
            flag = "3"
            if cmd == "251" and len(f) > 1 and f[1].isdigit():
                acks.append(sentence(f"PMTK001,{cmd},{flag}").encode() + b"\r\n")   # acked at the old baud
                self.baud = int(f[1])
                print(f"SIM baud {self.baud}", flush=True)
                continue
            if cmd == "220" and len(f) > 1 and f[1].isdigit():
                self.rate = min(1000 / max(int(f[1]), 100), self.max_rate)
                print(f"SIM rate {self.rate:g} Hz", flush=True)
            elif cmd == "314":
                vals = f[1:]
                self.enabled = {n for n, v in zip(PMTK314_FIELDS, vals) if v.strip() not in ("", "0")}
                print(f"SIM sentences {sorted(self.enabled)}", flush=True)
            elif cmd == "353" and len(f) > 3:
                self.galileo = f[3] == "1"
                print(f"SIM constellations galileo={self.galileo}", flush=True)
            else:
                flag = "1"
            acks.append(sentence(f"PMTK001,{cmd},{flag}").encode() + b"\r\n")
        return acks


def span(s: str | None) -> tuple[int, int] | None:
    if not s:
        return None
    a, b = s.split(":")
    return int(a), int(b)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fixture", default=str(FIXTURE))
    ap.add_argument("--transport", choices=["auto", "pty", "tcp"], default="auto")
    ap.add_argument("--tcp-port", type=int, default=0, help="0 = any free port")
    ap.add_argument("--speed", "--rate", dest="speed", type=float, default=1.0,
                    help="simulated seconds per wall second")
    ap.add_argument("--start", default=None, help="simulated start time, ISO UTC (default: now)")
    ap.add_argument("--duration-s", type=int, default=None, help="simulated seconds to send (default: fixture)")
    ap.add_argument("--baud", type=int, default=9600, help="power-on baud of the module")
    ap.add_argument("--max-rate", type=float, default=10.0, help="highest output rate the module accepts")
    ap.add_argument("--wait-client-s", type=float, default=30.0, help="tcp: wait this long for the first client")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--walk", default=None, help="loop:W:H:MPS rectangle walk (default: static)")
    ap.add_argument("--jam", choices=["ramp", "sudden"], default=None)
    ap.add_argument("--jam-at", type=float, default=120.0)
    ap.add_argument("--jam-rate-db-s", type=float, default=4.0)
    ap.add_argument("--jam-depth-db", type=float, default=25.0)
    ap.add_argument("--spoof", choices=["jump", "drift", "time"], default=None)
    ap.add_argument("--spoof-at", type=float, default=120.0)
    ap.add_argument("--jump-m", type=float, default=500.0)
    ap.add_argument("--drift-mps", type=float, default=1.0)
    ap.add_argument("--time-offset-s", type=float, default=30.0)
    ap.add_argument("--spoof-cn0", choices=["uniform", "natural"], default="uniform")
    ap.add_argument("--recover-after", type=float, default=None, help="the jam/spoof ends DUR s after it began")
    ap.add_argument("--galileo", action="store_true", help="start with Galileo on (as if PMTK353 had been sent)")
    ap.add_argument("--write", default=None, help="render the whole run into this file (host-prefixed) and exit")
    ap.add_argument("--write-rate", type=float, default=1.0, help="--write: output rate in Hz")
    ap.add_argument("--inject-fade", default=None, help="PRN:START:DUR:DEPTH")
    ap.add_argument("--inject-dropout", default=None, help="START:DUR")
    ap.add_argument("--inject-garbage", default="", help="T[,T...]")
    ap.add_argument("--inject-disconnect", default=None, help="START:DUR (tcp)")
    return ap


def write_file(a, cycles, start) -> int:
    """Render the run into a host-prefixed NMEA file; host time = true time + ~0.3 s receive delay."""
    scen = Scenario(cycles, a, start)
    fade = tuple(int(x) for x in a.inject_fade.split(":")) if a.inject_fade else None
    total = a.duration_s or len(cycles)
    n = max(1, int(round(a.write_rate)))
    enabled = {"RMC", "GGA", "GSA", "GSV", "VTG"} if n == 1 else {"RMC", "GGA", "GSA", "GSV"}
    rng = random.Random(a.seed + 1)
    lines = []
    for k in range(total):
        for j in range(n):
            s = k + j / n
            host = start + timedelta(seconds=s + 0.3 + rng.uniform(0, 0.05))
            hp = f"{host:%Y-%m-%dT%H:%M:%S}.{host.microsecond // 1000:03d}Z"
            sents = scen.render(k, s, enabled, a.galileo, fade)
            if k == 0 and j == 0:
                sents = [sentence("GPTXT,01,01,02,ANTSTATUS=OK")] + sents
            lines += [f"{hp},{x}" for x in sents]
    Path(a.write).write_text("\n".join(lines) + "\n", encoding="ascii")
    return total * n


def main(argv=None):
    a = build_parser().parse_args(argv)
    cycles = load_cycles(Path(a.fixture))
    start = (datetime.fromisoformat(a.start.replace("Z", "+00:00")) if a.start
             else datetime.now(timezone.utc).replace(microsecond=0))
    if a.write:
        n = write_file(a, cycles, start)
        print(f"wrote {a.write}: {n} epochs from {start:%Y-%m-%dT%H:%M:%SZ}", flush=True)
        return
    total = a.duration_s or len(cycles)
    fade = tuple(int(x) for x in a.inject_fade.split(":")) if a.inject_fade else None
    dropout, disconnect = span(a.inject_dropout), span(a.inject_disconnect)
    garbage_at = {int(x) for x in a.inject_garbage.split(",") if x.strip()}
    mod = Module(a.baud, a.max_rate)
    mod.galileo = a.galileo
    scen = Scenario(cycles, a, start)

    kind = a.transport
    if kind == "auto":
        kind = "pty" if hasattr(os, "openpty") and sys.platform != "win32" else "tcp"
    tr = PtyTransport() if kind == "pty" else TcpTransport(a.tcp_port)
    print(f"PORT {tr.path}", flush=True)

    def service():
        for ack in mod.command(tr.poll()):
            tr.send(ack)

    if kind == "tcp":                      # start the clock when the reader is there
        t_end = time.monotonic() + a.wait_client_s
        while not tr.connected() and time.monotonic() < t_end:
            service()
            time.sleep(0.01)
    wall0 = time.monotonic()
    try:
        for k in range(total):
            if disconnect and k == disconnect[0]:
                tr.refuse = kind == "tcp"
                tr.disconnect()
                mod.reset()                                  # power lost: settings gone
                print(f"SIM {k}s disconnect (settings reset)", flush=True)
            if disconnect and k == disconnect[0] + disconnect[1]:
                tr.refuse = False
                print(f"SIM {k}s reconnect allowed", flush=True)
            silent = dropout and dropout[0] <= k < dropout[0] + dropout[1]
            budget = mod.baud / 10                          # bytes this second (8N1)
            n = max(1, int(round(mod.rate)))
            for j in range(n):
                target = wall0 + (k + j / n) / a.speed
                while (rem := target - time.monotonic()) > 0:
                    service()
                    time.sleep(min(rem, 0.005))
                service()
                s = k + j / n
                sents = scen.render(k, s, mod.enabled, mod.galileo, fade)   # state advances even when silent
                if silent:
                    continue
                if j == 0 and k in garbage_at:
                    for g in garbage(start + timedelta(seconds=s)):
                        tr.send(g)
                if mod.banner:
                    sents = [sentence("GPTXT,01,01,02,ANTSTATUS=OK")] + sents
                    mod.banner = False
                data = ("\r\n".join(sents) + "\r\n").encode()
                if budget <= 0:
                    continue
                if len(data) > budget:                      # the line is full: the epoch is cut off
                    data = data[:int(budget)]
                budget -= len(data)
                tr.send(data)
    except KeyboardInterrupt:
        pass
    finally:
        print("SIM done", flush=True)
        time.sleep(0.2)
        tr.close()


if __name__ == "__main__":
    main()
