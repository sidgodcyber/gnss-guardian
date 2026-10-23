"""One-line health check of a live (or replay) session folder: the terminal backup for the dashboard.

    gnss-guardian status --data session/live          # one line per second, Ctrl-C to stop

    14:00:05 IST | GNSS TRUSTED | fix 9/11 | C/N0 38.4 sd 5.0 | HDOP 0.9 | jam CLEAR 0.00 | spoof CLEAR 0.05 | home 12 m | age 0.4 s
    14:02:09 IST | GNSS denied: jamming or blockage | fix lost | ... | jam ALARM 0.94 (C/N0 down >= 6 dB on 12/12 satellites; ...)
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
    """(line, stale). stale when latest.json is missing or older than STALE_S seconds."""
    folder = Path(folder)
    now = now or datetime.now(timezone.utc)
    clock = f"{now.astimezone(ZoneInfo(tz)):%H:%M:%S} IST"
    v, age = _latest(folder)
    if v is None:
        return f"{clock} | STALE: no epochs yet in {folder} (is live mode running?)", True
    f = v["features"]
    if v["warming_up"]:
        head = "WARMING UP"
    elif v["labels"]:
        head = " + ".join(v["labels"])
    else:
        head = "GNSS TRUSTED" if v["gnss_trusted"] else "GNSS NOT TRUSTED"
    fix = f"fix {f['n_used']}/{f['n_tracked']}" if v["fix_valid"] else f"fix lost ({f['n_tracked']} tracked)"
    cn0 = (f"C/N0 {f['cn0_mean']:.1f} sd {f['cn0_std'] or 0:.1f}" if f.get("cn0_mean") is not None else "C/N0 -")
    hdop = f"HDOP {v['hdop']:.1f}" if v.get("hdop") is not None else "HDOP -"
    parts = [clock, head, fix, cn0, hdop]
    for d, short in (("jamming", "jam"), ("spoofing", "spoof")):
        s = f"{short} {v[d]['level']} {v[d]['score']:.2f}"
        if v[d]["level"] != "CLEAR" and v[d]["reasons"]:
            s += f" ({'; '.join(v[d]['reasons'])})"
        parts.append(s)
    if v.get("enu_m"):
        parts.append(f"home {math.hypot(*v['enu_m']):.0f} m")
    if v.get("antenna") not in (None, "OK"):
        parts.append(f"ANTENNA {v['antenna']}")
    parts.append(f"age {age:.1f} s")
    line = " | ".join(parts)
    if age > STALE_S:
        return f"{clock} | STALE: last epoch {age:.0f} s old (receiver silent or live mode stopped) | {line}", True
    return line, False


def watch(folder: str | Path, every_s: float = 1.0, count: int | None = None, echo=print, tz: str = "Asia/Kolkata"):
    n = 0
    while count is None or n < count:
        echo(status_line(folder, tz)[0])
        n += 1
        if count is not None and n >= count:
            break
        time.sleep(every_s)
