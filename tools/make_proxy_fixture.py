"""One-off: turn a phone GnssLogger log into an L86-style, host-prefixed NMEA file (offline test
data only; the phone is never a runtime input). Needs an IonWatch checkout for its GnssLogger parser:

    python tools/make_proxy_fixture.py --ionwatch ../ionwatch LOG.txt OUT.nmea [--cn0-offset-db 8] [--max-min 30]

GPS L1 -> GPGSV, GLONASS G1 -> GLGSV (the L86's default constellations), C/N0 + offset (an L86 with an
outdoor antenna sees more than a phone indoors; IonWatch used 8 dB). Position, speed and course come
from the phone's own GPS fixes (FLP if no GPS fix that second); no fix -> RMC V. HDOP is modelled from
the number of satellites used. Host prefix = phone time + 0.3 s. A proxy for real L86 data, labelled so.
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

L1 = {"GPS_L1": ("GP", 0), "GLO_G1": ("GL", 64)}


def nmea(body: str) -> str:
    x = 0
    for ch in body:
        x ^= ord(ch)
    return f"${body}*{x:02X}"


def ll(lat, lon):
    def fmt(x, w):
        a = abs(x)
        d = int(a)
        return f"{d:0{w}d}{(a - d) * 60:07.4f}"
    return fmt(lat, 2), "N" if lat >= 0 else "S", fmt(lon, 3), "E" if lon >= 0 else "W"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log")
    ap.add_argument("out")
    ap.add_argument("--ionwatch", required=True)
    ap.add_argument("--cn0-offset-db", type=float, default=8.0)
    ap.add_argument("--max-min", type=float, default=None)
    a = ap.parse_args()
    sys.path.insert(0, str(Path(a.ionwatch).resolve()))
    import pandas as pd
    from ionwatch.io import gnsslogger
    from ionwatch.proc.common import load_config
    cfg = load_config()
    log = gnsslogger.read_phone([a.log], cfg)
    obs, _ = gnsslogger.cn0_obs(log, cfg)
    o = obs[obs["signal"].isin(L1) & obs["elev_deg"].notna() & (obs["elev_deg"] >= 0)].copy()
    o["sec"] = o["time_utc"].dt.floor("s")
    fx = log.fix[log.fix["LatitudeDegrees"].notna() & log.fix["Provider"].isin(["GPS", "FLP"])].copy()
    fx["sec"] = fx["time_utc"].dt.round("s")
    fx = fx.sort_values(["sec", "Provider"], key=lambda s: s.map({"GPS": 0, "FLP": 1}) if s.name == "Provider" else s)
    fixes = fx.drop_duplicates("sec").set_index("sec")
    t0, t1 = o["sec"].min(), o["sec"].max()
    if a.max_min:
        t1 = min(t1, t0 + pd.Timedelta(minutes=a.max_min))
    by_sec = dict(tuple(o[(o["sec"] >= t0) & (o["sec"] <= t1)].groupby("sec")))
    lines, n_fix = [], 0
    for t in pd.date_range(t0, t1, freq="1s"):
        host = t + pd.Timedelta(milliseconds=300)
        hp = f"{host:%Y-%m-%dT%H:%M:%S.%f}"[:-3] + "Z"
        hms, dmy = f"{t:%H%M%S}.000", f"{t:%d%m%y}"
        sky = {"GP": [], "GL": []}
        sec = by_sec.get(t)
        if sec is not None:
            for r in sec.drop_duplicates(["signal", "svid"]).itertuples():
                tk, base = L1[r.signal]
                cn0 = None if pd.isna(r.cn0_dbhz) or r.cn0_dbhz <= 0 else int(round(min(r.cn0_dbhz + a.cn0_offset_db, 99)))
                sky[tk].append((base + int(r.svid), int(round(r.elev_deg)), int(round(r.az_deg)) % 360, cn0))
        used = {tk: [p for p, el, _, c in s if c is not None and c >= 25 and el >= 10][:12] for tk, s in sky.items()}
        n_used = sum(len(v) for v in used.values())
        f = fixes.loc[t] if t in fixes.index else None
        out = []
        if f is not None and n_used >= 4:
            n_fix += 1
            la, ns, lo, ew = ll(f["LatitudeDegrees"], f["LongitudeDegrees"])
            sog = (f["SpeedMps"] if not pd.isna(f["SpeedMps"]) else 0.0) / 0.514444
            cog = f["BearingDegrees"] if not pd.isna(f["BearingDegrees"]) else 0.0
            alt = f["AltitudeMeters"] if not pd.isna(f["AltitudeMeters"]) else 0.0
            hdop = 0.9 * math.sqrt(10 / n_used)
            out += [f"GPRMC,{hms},A,{la},{ns},{lo},{ew},{sog:.2f},{cog:.2f},{dmy},,,A",
                    f"GPGGA,{hms},{la},{ns},{lo},{ew},1,{n_used:02d},{hdop:.2f},{alt:.1f},M,-40.0,M,,"]
            for tk in ("GP", "GL"):
                prns = [f"{p:02d}" for p in used[tk]]
                out.append(f"{tk}GSA,A,3,{','.join(prns + [''] * (12 - len(prns)))},{hdop * 1.6:.2f},{hdop:.2f},{hdop * 1.4:.2f}")
        else:
            out += [f"GPRMC,{hms},V,,,,,0.00,0.00,{dmy},,,N", f"GPGGA,{hms},,,,,0,00,,,M,,M,,", "GPGSA,A,1,,,,,,,,,,,,,,,"]
        for tk in ("GP", "GL"):
            s = sky[tk]
            chunks = [s[i:i + 4] for i in range(0, len(s), 4)] or [[]]
            for i, ch in enumerate(chunks, 1):
                body = f"{tk}GSV,{len(chunks)},{i},{len(s):02d}"
                if ch:
                    body += "," + ",".join(f"{p:02d},{el:02d},{az:03d},{'' if c is None else f'{c:02d}'}" for p, el, az, c in ch)
                out.append(body)
        lines += [f"{hp},{nmea(b)}" for b in out]
    Path(a.out).write_text("\n".join(lines) + "\n", encoding="ascii")
    n = int((t1 - t0).total_seconds()) + 1
    print(f"wrote {a.out}: {n} s from {t0:%Y-%m-%dT%H:%M:%SZ}, fix in {n_fix} s")


if __name__ == "__main__":
    main()
