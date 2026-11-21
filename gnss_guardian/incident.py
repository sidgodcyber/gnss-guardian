"""Incident reports for a session, with a SHA-256 hash chain over its epochs.jsonl so edits are detectable.

    gnss-guardian incident-report session/road1     -> session/road1/incidents/incident_N.md, incidents.json,
                                                       chain.json
    gnss-guardian incident-verify session/road1     -> OK, or where the log or a report was changed

An incident is one alarm episode of the state machine: from the epoch the state becomes JAMMED or SPOOFED
until GNSS is trusted again (or the session ends: "ongoing"). Per incident: UTC start / end and duration;
type (jamming or blockage / spoofing) and the detector's indicators and reasons; the AI second opinion at the
start with its model label; positions (last trusted fix before, the GNSS-reported fix at the start, the
fused position at the end); the state chain; the affected equipment; whether a demo injection was active
(then it is NOT a real attack); and the session logs with their SHA-256.

Latitude / longitude appear only in these local report files, never in dashboard.json or live.json.

Report fields: time, location, interference type, affected equipment, logs. These follow what the DGCA
advisory circular on GNSS interference reporting asks for as far as we could tell; the field list is NOT
verified against the official form.

Hash chain: h0 = SHA-256("gnss-guardian-chain/1"), h_i = SHA-256(h_{i-1} || line_i) over the bytes of each
line of epochs.jsonl (without the newline). chain.json keeps the line count, the head, h at every STRIDE
lines (so verify can say where an edit is), h at each incident's last epoch, and the SHA-256 of each
report file.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from gnss_guardian import geo
from gnss_guardian.session import read_jsonl

SEED = b"gnss-guardian-chain/1"
STRIDE = 60
ALARM_STATES = ("JAMMED", "SPOOFED")
TYPE = {"jamming": "jamming or blockage (signal denial: C/N0 down, satellites lost)",
        "spoofing": "spoofing (false GNSS signals: position, time or sky inconsistent)"}
CONST = {"G": "GPS", "R": "GLONASS", "E": "Galileo", "C": "BeiDou", "J": "QZSS", "S": "SBAS"}
RECEIVER = "Quectel L86-M33 GNSS module (MediaTek MT3333), NMEA over UART"
DGCA_NOTE = ("Fields: time, location, interference type, affected equipment, logs. Chosen to match the DGCA "
             "advisory circular on GNSS interference reporting as far as we could tell; NOT verified against "
             "the official form.")


def _lines(path: Path) -> list[bytes]:
    return [ln.rstrip(b"\r") for ln in path.read_bytes().split(b"\n") if ln.strip()]


def chain(lines: list[bytes]) -> list[str]:
    """h_1 .. h_n (hex) over the lines."""
    h, out = hashlib.sha256(SEED).digest(), []
    for ln in lines:
        h = hashlib.sha256(h + ln).digest()
        out.append(h.hex())
    return out


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ll(lat, lon):
    return None if lat is None or lon is None else {"lat": round(lat, 7), "lon": round(lon, 7)}


def find_incidents(eps: list[dict], events: list[dict], home: dict | None) -> list[dict]:
    inj = [(e["mono"], e.get("action"), e.get("label") or e.get("type")) for e in events
           if e["kind"] == "demo_inject" and e.get("mono") is not None]
    out, cur, last_trusted = [], None, None
    for i, v in enumerate(eps):
        st = v.get("state")
        if cur is None:
            if v.get("gnss_trusted") and v.get("lat") is not None:
                last_trusted = v
            if st in ALARM_STATES:
                cur = {"start": i, "chain": [st], "last_trusted": last_trusted}
            continue
        if st and st != cur["chain"][-1]:
            cur["chain"].append(st)
        if v.get("gnss_trusted"):
            cur["end"] = i
            out.append(cur)
            cur = None
            last_trusted = v
    if cur is not None:
        cur["end"] = None
        out.append(cur)

    incidents = []
    for k, c in enumerate(out, 1):
        s = eps[c["start"]]
        e = eps[c["end"]] if c["end"] is not None else eps[-1]
        cause = s.get("cause") or ("spoofing" if s["spoofing"]["level"] == "ALARM" else "jamming")
        span = eps[c["start"]:(c["end"] if c["end"] is not None else len(eps))]
        reasons, parts = [], {}
        for v in span:
            for r in v[cause]["reasons"]:
                if r not in reasons:
                    reasons.append(r)
            for p, x in v[cause]["parts"].items():
                parts[p] = max(parts.get(p, 0.0), x)
        fu = (eps[c["end"] - 1] if c["end"] else e).get("fused")
        fused_ll = None
        if fu and home:
            fused_ll = {**_ll(*geo.from_enu(fu["e"], fu["n"], home["lat"], home["lon"])),
                        "source": fu["source"], "home_distance_m": fu.get("home_distance_m")}
        t0 = s.get("mono")
        demo = None
        for m, action, label in inj:
            if t0 is not None and m <= t0:
                demo = None if action == "stop" else label
        lt = c["last_trusted"]
        dur = (e["mono"] - s["mono"]) if (s.get("mono") is not None and e.get("mono") is not None) else None
        incidents.append({
            "id": k, "type": cause, "interference_type": TYPE[cause],
            "demo_injection": demo,
            "start_utc": s.get("t_host") or s.get("t_gnss"), "start_gnss_time": s.get("t_gnss"),
            "end_utc": (e.get("t_host") or e.get("t_gnss")) if c["end"] is not None else None,
            "ongoing_at_session_end": c["end"] is None, "duration_s": round(dur, 1) if dur is not None else None,
            "location": {"last_trusted_fix": {**_ll(lt["lat"], lt["lon"]), "utc": lt.get("t_host") or lt.get("t_gnss")}
                         if lt else None,
                         "gnss_reported_at_start": _ll(s.get("lat"), s.get("lon")),
                         "fused_at_end": fused_ll, "home": _ll(home["lat"], home["lon"]) if home else None},
            "detector": {"max_score": round(max(v[cause]["score"] for v in span), 3),
                         "indicators": {p: round(x, 3) for p, x in sorted(parts.items(), key=lambda kv: -kv[1]) if x >= 0.3},
                         "reasons": reasons[:12]},
            "ai_at_start": s.get("ai"),
            "state_chain": c["chain"] + (["GNSS_OK"] if c["end"] is not None and c["chain"][-1] != "GNSS_OK" else []),
            "epochs": [s["seq"], e["seq"]], "epoch_lines": [c["start"] + 1, (c["end"] or len(eps)) + 1],
            "constellations": sorted({CONST.get(key[0], key[0]) for key in s.get("sats", {})}),
        })
    return incidents


def _md(inc: dict, info: dict, session: Path, logs: dict, head: str) -> str:
    L = inc["location"]

    def ll(x):
        return f"{x['lat']:.7f}, {x['lon']:.7f}" if x else "–"
    rows = [f"# GNSS interference incident {inc['id']}: {inc['type'].upper()}", ""]
    if inc["demo_injection"]:
        rows += [f"**DEMO INJECTION ACTIVE ({inc['demo_injection']}): software injection before the detector, "
                 f"NOT a real attack.**", ""]
    rows += ["| Field | Value |", "|---|---|",
             f"| Time (UTC) | {inc['start_utc']} → {inc['end_utc'] or 'ongoing at session end'}"
             f" ({inc['duration_s']} s) |",
             f"| GNSS time at start | {inc['start_gnss_time']} |",
             f"| Location: last trusted fix | {ll(L['last_trusted_fix'])} |",
             f"| Location: GNSS-reported at start (may be false) | {ll(L['gnss_reported_at_start'])} |",
             f"| Location: fused at end ({(L['fused_at_end'] or {}).get('source', '–')}) | {ll(L['fused_at_end'])} |",
             f"| Interference type | {inc['interference_type']} |",
             f"| Detector max score | {inc['detector']['max_score']} |",
             f"| Indicators (≥ 0.3) | {', '.join(f'{k} {v}' for k, v in inc['detector']['indicators'].items()) or '–'} |",
             f"| AI second opinion at start | "
             + (f"score {inc['ai_at_start']['score']}, level {inc['ai_at_start']['level']} (advisory; model: "
                f"{inc['ai_at_start']['model']})"
                if inc["ai_at_start"] else "no AI model") + " |",
             f"| State chain | {' → '.join(inc['state_chain'])} |",
             f"| Affected equipment | node {info.get('node_id')}; {RECEIVER}; constellations "
             f"{', '.join(inc['constellations'])} |",
             f"| Session | {session.name} ({info.get('mode')}), epochs {inc['epochs'][0]}–{inc['epochs'][1]} "
             f"(epochs.jsonl lines {inc['epoch_lines'][0]}–{inc['epoch_lines'][1]}) |", "",
             "Reasons:", ""] + [f"- {r}" for r in inc["detector"]["reasons"]] + [
             "", "Logs (SHA-256):", ""] + [f"- `{k}` {v}" for k, v in logs.items()] + [
             "", f"Hash-chain head of epochs.jsonl: `{head}` (check: gnss-guardian incident-verify {session})", "",
             f"_{DGCA_NOTE}_", ""]
    return "\n".join(rows)


def report(session: str | Path, out: str | Path | None = None) -> dict:
    session = Path(session)
    out = Path(out) if out else session / "incidents"
    out.mkdir(parents=True, exist_ok=True)
    info = json.loads((session / "session.json").read_text(encoding="utf-8"))
    ep_path = session / "epochs.jsonl"
    eps = read_jsonl(ep_path)
    events = read_jsonl(session / "events.jsonl") if (session / "events.jsonl").exists() else []
    incs = find_incidents(eps, events, info.get("home"))
    hs = chain(_lines(ep_path))
    logs = {p.relative_to(session).as_posix(): _sha(p) for p in
            [ep_path, session / "events.jsonl", *sorted(session.glob("raw/*.nmea")),
             *sorted(session.glob("detector_input/*.nmea"))] if p.exists()}
    written = {}
    for inc in incs:
        p = out / f"incident_{inc['id']}.md"
        p.write_text(_md(inc, info, session, logs, hs[-1] if hs else ""), encoding="utf-8")
        written[p.name] = _sha(p)
    doc = {"format": "gnss-guardian-incidents/1", "session": session.name, "node_id": info.get("node_id"),
           "written_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "receiver": RECEIVER,
           "field_list_note": DGCA_NOTE, "logs_sha256": logs, "incidents": incs}
    jp = out / "incidents.json"
    jp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    written[jp.name] = _sha(jp)
    manifest = {"format": "gnss-guardian-chain/1", "file": "epochs.jsonl", "lines": len(hs),
                "head": hs[-1] if hs else None, "stride": STRIDE,
                "every": {str(i + 1): hs[i] for i in range(STRIDE - 1, len(hs), STRIDE)},
                "incidents": {str(i["id"]): {"line": i["epoch_lines"][1] - 1, "hash": hs[i["epoch_lines"][1] - 2]}
                              for i in incs},
                "reports": written}
    (out / "chain.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return {"incidents": incs, "out": str(out), "head": manifest["head"]}


def verify(session: str | Path, out: str | Path | None = None) -> tuple[bool, list[str]]:
    """(ok, messages): recompute the chain over epochs.jsonl and the report hashes against chain.json."""
    session = Path(session)
    out = Path(out) if out else session / "incidents"
    m = json.loads((out / "chain.json").read_text(encoding="utf-8"))
    hs = chain(_lines(session / m["file"]))
    msgs, ok = [], True
    if len(hs) < m["lines"]:
        ok = False
        msgs.append(f"epochs.jsonl has {len(hs)} lines, the chain recorded {m['lines']}: lines removed")
    good_to = 0
    for n, h in sorted(((int(k), v) for k, v in m["every"].items())):
        if n <= len(hs) and hs[n - 1] == h:
            good_to = n
        else:
            ok = False
            msgs.append(f"epochs.jsonl changed between lines {good_to + 1} and {n}")
            break
    if ok and (not hs or hs[m["lines"] - 1] != m["head"]):
        ok = False
        msgs.append(f"epochs.jsonl changed after line {good_to} (head differs)")
    if ok and len(hs) > m["lines"]:
        msgs.append(f"note: {len(hs) - m['lines']} lines appended after the report (the reported part is intact)")
    for name, sha in m["reports"].items():
        p = out / name
        if not p.exists():
            ok = False
            msgs.append(f"{name} missing")
        elif _sha(p) != sha:
            ok = False
            msgs.append(f"{name} was changed after it was written")
    if ok:
        msgs.insert(0, f"OK: {m['lines']} epochs, head {m['head'][:16]}..., {len(m['reports'])} report files intact")
    return ok, msgs
