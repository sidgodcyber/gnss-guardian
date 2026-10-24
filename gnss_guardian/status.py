"""One-line health check of a live (or replay) session folder: the terminal backup for the dashboard.

    gnss-guardian status --data session/live          # one line per second, Ctrl-C to stop

    14:00:05 IST | GNSS TRUSTED | GNSS_OK | fix 9/11 | C/N0 38.4 sd 5.0 | HDOP 0.9 | jam CLEAR 0.00 | spoof CLEAR 0.05 | home 12 m at 270 deg (gnss) | age 0.4 s
    14:02:09 IST | GNSS denied: jamming or blockage | RETURN_HOME | fix lost | ... | home 9 m at 268 deg (vision) | next waypoint 4: 1.8 m at 270 deg
    14:05:30 IST | STALE: last epoch 12 s old (receiver silent or live mode stopped) | GNSS UNKNOWN | last: fix 9/11 | ...
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

STALE_S = 5


def _latest(folder: Path) -> tuple[dict | None, float | None]:
    p = folder / "latest.json"
    if not p.exists() or p.stat().st_size == 0:
        return None, None
    try:
        return json.loads(p.read_text(encoding="utf-8")), time.time() - p.stat().st_mtime
    except ValueError:
        return None, None


def status_line(folder: str | Path, tz: str = "Asia/Kolkata", now: datetime | None = None) -> tuple[str, bool]:
    """(line, stale). stale when latest.json is missing or older than STALE_S seconds; the GNSS state is
    then UNKNOWN and the last epoch's values are shown after "last:"."""
    folder = Path(folder)
    now = now or datetime.now(timezone.utc)
    clock = f"{now.astimezone(ZoneInfo(tz)):%H:%M:%S} IST"
    v, age = _latest(folder)
    if v is None:
        return f"{clock} | STALE: no epochs yet in {folder} (is live mode running?)", True
    f = v["features"]
    stale = age > STALE_S
    if stale:
        head = "GNSS UNKNOWN"                        # the last verdict says nothing about the sky now
    elif v["warming_up"]:
        head = "WARMING UP"
    elif v["labels"]:
        head = " + ".join(v["labels"])
    else:
        head = "GNSS TRUSTED" if v["gnss_trusted"] else "GNSS NOT TRUSTED"
    fix = f"fix {f['n_used']}/{f['n_tracked']}" if v["fix_valid"] else f"fix lost ({f['n_tracked']} tracked)"
    cn0 = (f"C/N0 {f['cn0_mean']:.1f} sd {f['cn0_std'] or 0:.1f}" if f.get("cn0_mean") is not None else "C/N0 -")
    hdop = f"HDOP {v['hdop']:.1f}" if v.get("hdop") is not None else "HDOP -"
    parts = [v.get("state") or "-", fix, cn0, hdop]
    for d, short in (("jamming", "jam"), ("spoofing", "spoof")):
        s = f"{short} {v[d]['level']} {v[d]['score']:.2f}"
        if v[d]["level"] != "CLEAR" and v[d]["reasons"]:
            s += f" ({'; '.join(v[d]['reasons'])})"
        parts.append(s)
    fu, g = v.get("fused"), v.get("guidance")
    if fu:
        parts.append(f"home {fu['home_distance_m']:.0f} m at {fu['home_bearing_deg']:.0f} deg ({fu['source']})")
    elif v.get("enu_m"):
        parts.append(f"home {math.hypot(*v['enu_m']):.0f} m")
    if g:                                            # advisory guidance only: the module commands nothing
        w = g["waypoint"]
        parts.append(f"next waypoint {w['id']}: {w['distance_m']:.1f} m at {w['bearing_deg']:.0f} deg")
    if v.get("latch") and not v["latch"]["agreeing"]:
        parts.append(f"spoof latch: {v['latch']['detail']}")
    if v.get("antenna") not in (None, "OK"):
        parts.append(f"ANTENNA {v['antenna']}")
    if stale:
        return (f"{clock} | STALE: last epoch {age:.0f} s old (receiver silent or live mode stopped) | {head} | "
                f"last: {' | '.join(parts)}"), True
    return " | ".join([clock, head, *parts, f"age {age:.1f} s"]), False


def watch(folder: str | Path, every_s: float = 1.0, count: int | None = None, echo=print, tz: str = "Asia/Kolkata"):
    n = 0
    while count is None or n < count:
        echo(status_line(folder, tz)[0])
        n += 1
        if count is not None and n >= count:
            break
        time.sleep(every_s)
