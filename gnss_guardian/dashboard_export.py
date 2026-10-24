"""Dashboard replay file: one dashboard.json per session, written at the end of `replay`.

    session/<name>/dashboard.json         timeline (one row per second), state transitions, detector
                                          level changes, keyframes, return-home routes, events, indicator config
    session/<name>/dashboard_video.webm   the session video re-encoded for the browser (VP8), when
                                          replay ran with --video (or a live session recorded its
                                          overlay); rows carry the frame index

A live session folder (gnss-guardian live / field-demo) exports the same way, from what live mode
recorded (its own state machine and vision): gnss-guardian replay session/<name>. Demo injections
(demo_inject events) mark the rows they cover ("injection") and appear in the event list.

Positions are local metres from the home point (east, north). No latitude, longitude or altitude is
written: rows are built from an explicit list of fields.

The state, fused position, guidance and spoof-latch progress are copied from epochs.jsonl as the
pipeline's state machine (gnss_guardian/state.py) wrote them; nothing is re-derived here. Transition
reasons come from the "state" events.
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

from gnss_guardian.session import read_jsonl

FORMAT = "gnss-guardian-dashboard/2"
STATE_SOURCE = "pipeline state machine (gnss_guardian/state.py), read from epochs.jsonl"
FEATURES = ("drop_frac", "mean_drop_db", "lost_frac", "hdop_ratio", "fix_lost", "implied_speed_mps", "sog_mps",
            "time_jump_s", "alt_rate_mps", "cn0_std", "newcomer_frac", "vision_diff_mps", "vision_drift_mps",
            "vision_pos_drift_m")
VISION = ("mode", "e", "n", "heading", "aligned", "quality", "n_inliers", "flag", "v_e", "v_n", "keyframes",
          "home_dist_m")


def indicator_config(cfg: dict) -> dict:
    j, s, h = cfg["detect"]["jamming"], cfg["detect"]["spoofing"], cfg["detect"]["hysteresis"]
    vmax = cfg["rig"]["max_speed_mps"]

    def ind(key, label, feature, span, unit, weights):
        return {"key": key, "label": label, "feature": feature, "span": span, "unit": unit, "weight": weights[key]}

    return {
        "jamming": {"combine": "weighted sum", "indicators": [
            ind("drop_frac", f"satellites with C/N0 >= {j['drop_db']} dB below baseline", "drop_frac",
                j["drop_frac"], "fraction", j["weights"]),
            ind("mean_drop", "mean C/N0 drop", "mean_drop_db", j["mean_drop_db"], "dB", j["weights"]),
            ind("lost_frac", "satellites lost", "lost_frac", j["lost_frac"], "fraction", j["weights"]),
            ind("hdop", "HDOP rise against baseline", "hdop_ratio", j["hdop_ratio"], "x baseline", j["weights"]),
            ind("fix_lost", "fix lost", "fix_lost", None, "", j["weights"]),
        ]},
        "spoofing": {"combine": "noisy-OR", "indicators": [
            ind("speed", f"position jump / speed above rig max ({vmax:g} m/s)", "implied_speed_mps",
                [x * vmax for x in s["speed_ramp_x"]], "m/s", s["weights"]),
            ind("time", "GNSS time jump against host clock", "time_jump_s", s["time_jump_s"], "s", s["weights"]),
            ind("altitude", "altitude rate", "alt_rate_mps", s["alt_rate_mps"], "m/s", s["weights"]),
            ind("uniform_cn0", f"uniform strong C/N0 (mean {s['uniform_mean_dbhz'][0]}-{s['uniform_mean_dbhz'][1]} dB-Hz)",
                "cn0_std", s["uniform_std_db"], "dB std", s["weights"]),
            ind("newcomers", "tracked satellites not in baseline", "newcomer_frac", s["newcomer_frac"], "fraction",
                s["weights"]),
            ind("vision", f"GNSS vs camera velocity ({s['vision_window_s']} s / {s['vision_long_window_s']} s)",
                "vision_diff_mps", s["vision_diff_mps"], "m/s", s["weights"]),
            ind("vision_pos", f"GNSS vs camera displacement ({s['vision_pos_window_s']} s)", "vision_pos_drift_m",
                s["vision_pos_drift_m"], "m", s["weights"]),
        ]},
        "hysteresis": {k: h[k] for k in ("suspect_on", "suspect_off", "suspect_hold_s", "alarm_on", "alarm_k",
                                         "alarm_immediate", "alarm_off", "alarm_hold_s", "min_alarm_s")},
    }


def _host_t(v: dict) -> float | None:
    if v.get("mono") is not None:
        return float(v["mono"])
    try:
        return datetime.fromisoformat(v["t_gnss"].replace("Z", "+00:00")).timestamp()
    except (KeyError, TypeError, ValueError):
        return None


def _r(x, nd=3):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(x, nd)


def _detector(d: dict) -> dict:
    return {"level": d["level"], "score": _r(d["score"]), "parts": {k: _r(p) for k, p in d["parts"].items()},
            "reasons": d["reasons"]}


def _row(v: dict, t: float) -> dict:
    f = v.get("features") or {}
    vis = v.get("vision")
    enu = v.get("enu_m")
    return {
        "t": _r(t), "t_gnss": v.get("t_gnss"), "seq": v.get("seq"), "state": v.get("state"), "cause": v.get("cause"),
        "warming_up": v.get("warming_up"), "gnss_trusted": v.get("gnss_trusted"),
        "gnss": {"fix_valid": v.get("fix_valid"), "sats_used": f.get("n_used"), "sats_tracked": f.get("n_tracked"),
                 "hdop": v.get("hdop"), "cn0_mean": _r(f.get("cn0_mean"), 1),
                 "e": enu[0] if enu else None, "n": enu[1] if enu else None},
        "jamming": _detector(v["jamming"]), "spoofing": _detector(v["spoofing"]),
        "features": {k: _r(f.get(k)) if not isinstance(f.get(k), bool) else f.get(k) for k in FEATURES},
        "vision": {k: vis.get(k) for k in VISION} if vis else None,
        "fused": v.get("fused"), "guidance": v.get("guidance"), "latch": v.get("latch"),
        "frame": None,
    }


VIDEO_MAX_WIDTH = 320


def _transcode(video: Path, frame_times: list[float], out: Path) -> float:
    """Re-encode to VP8 WebM (browsers cannot play MJPG AVI) at the recording's frame rate, scaled
    down to VIDEO_MAX_WIDTH for a light preview."""
    import cv2

    dts = sorted(b - a for a, b in zip(frame_times, frame_times[1:]) if b > a)
    fps = 1.0 / dts[len(dts) // 2] if dts else 30.0
    cap = cv2.VideoCapture(str(video))
    w = h = writer = None
    while True:
        ok, img = cap.read()
        if not ok:
            break
        if img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        if img.shape[1] > VIDEO_MAX_WIDTH:
            img = cv2.resize(img, (VIDEO_MAX_WIDTH, round(img.shape[0] * VIDEO_MAX_WIDTH / img.shape[1])),
                             interpolation=cv2.INTER_AREA)
        if writer is None:
            h, w = img.shape[:2]
            writer = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"VP80"), fps, (w, h), True)
            if not writer.isOpened():
                raise RuntimeError("OpenCV cannot write VP8 WebM on this machine")
        writer.write(img)
    cap.release()
    if writer is not None:
        writer.release()
    return round(fps, 3)


def _frame_times(video: Path, frames_csv: Path | None, video_start) -> list[float]:
    import cv2

    from gnss_guardian.vision.camera import read_frames_csv
    csv_path = Path(frames_csv) if frames_csv else video.with_name("frames.csv")
    if csv_path.exists():
        return read_frames_csv(csv_path)
    cap = cv2.VideoCapture(str(video))
    n, fps = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)), cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.release()
    t0 = video_start.timestamp()
    return [t0 + i / fps for i in range(n)]


def _injection_text(ev: dict) -> str:
    if ev["action"] == "stop":
        return "demo injection stopped"
    size = f"{ev.get('m')} m" if ev["action"] == "jump" else f"{ev.get('mps')} m/s"
    return (f"DEMO INJECTION {ev['action']} {size} at {ev.get('bearing_deg')} deg (software, before the detector; "
            f"not a real attack)")


def _injections(events: list[dict], t0: float) -> list[tuple[float, float, dict | None]]:
    """(t_start, t_end, injection) from the demo_inject events, t relative to the first epoch."""
    out, cur = [], None
    for ev in events:
        if ev["kind"] != "demo_inject" or ev.get("mono") is None:
            continue
        t = ev["mono"] - t0
        if cur is not None:
            out.append((cur[0], t, cur[1]))
            cur = None
        if ev["action"] != "stop":
            cur = (t, {k: ev.get(k) for k in ("id", "type", "bearing_deg", "m", "mps")})
    if cur is not None:
        out.append((cur[0], math.inf, cur[1]))
    return out


def _injection_at(spans, t: float) -> dict | None:
    for a, b, inj in spans:
        if a <= t < b:
            d = inj["m"] if inj["type"] == "jump" else round(inj["mps"] * (t - a), 1)
            return {**inj, "elapsed_s": round(t - a, 1), "offset_m": d}
    return None


def export_dashboard(session: str | Path, cfg: dict, video: str | Path | None = None,
                     frames_csv: str | Path | None = None, video_start=None) -> dict:
    session = Path(session)
    info = json.loads((session / "session.json").read_text(encoding="utf-8"))
    epochs = read_jsonl(session / "epochs.jsonl")
    events = read_jsonl(session / "events.jsonl")
    host = [_host_t(v) for v in epochs]
    t0 = next((t for t in host if t is not None), 0.0)
    rel = [None if t is None else t - t0 for t in host]
    by_gnss, by_seq = {}, {}
    for v, t in zip(epochs, rel):
        by_gnss.setdefault(v.get("t_gnss"), t)
        by_seq.setdefault(v.get("seq"), t)
    state_events = {e["seq"]: e for e in events if e["kind"] == "state"}
    injections = _injections(events, t0)

    timeline, transitions, levels = [], [], []
    prev_state, last_row_t = "__start__", None
    for v, t in zip(epochs, rel):
        if t is None:
            continue
        state = v.get("state")
        if state != prev_state:
            active = [d for d in ("jamming", "spoofing") if v[d]["level"] != "CLEAR"]
            transitions.append({"t": _r(t), "t_gnss": v.get("t_gnss"), "state": state,
                                "from": None if prev_state == "__start__" else prev_state, "cause": v.get("cause"),
                                "reasons": state_events.get(v.get("seq"), {}).get("reasons", []),
                                "indicators": [f"{d}.{k}" for d in active for k, p in v[d]["parts"].items() if p and p > 0]})
            prev_state = state
        for d in ("jamming", "spoofing"):
            if v[d].get("changed"):
                levels.append({"t": _r(t), "t_gnss": v.get("t_gnss"), "detector": d, "level": v[d]["level"],
                               "score": _r(v[d]["score"]), "reasons": v[d]["reasons"]})
        if last_row_t is None or t - last_row_t >= 0.9:    # about one row per second at 1/5/10 Hz
            row = _row(v, t)
            row["injection"] = _injection_at(injections, t)
            timeline.append(row)
            last_row_t = t

    keyframes, routes, other = [], [], []
    for ev in events:
        if ev.get("seq") is not None:
            t = by_seq.get(ev["seq"])
        elif ev.get("mono") is not None and ev.get("t_gnss") is None:
            t = ev["mono"] - t0
        else:
            t = by_gnss.get(ev.get("t_gnss"))
        t = _r(t) if t is not None else None
        kind = ev["kind"]
        if kind == "keyframe":
            keyframes.append({"t": t, "id": ev["id"], "e": ev["e"], "n": ev["n"], "home": ev.get("home", False)})
        elif kind == "guidance":
            routes.append({"t": t, "waypoints": ev["route"]})
            other.append({"t": t, "kind": kind, "detail": f"return-home route: {len(ev['route'])} waypoints "
                                                          f"back along the stored keyframes (advisory)"})
        elif kind == "waypoint":
            other.append({"t": t, "kind": kind, "detail": f"passed waypoint {ev['id']} ({ev['e']}, {ev['n']} m)"})
        elif kind == "alarm_escalated":
            other.append({"t": t, "kind": kind, "detail": f"alarm cause now {ev['cause']}: {'; '.join(ev['reasons'])}"})
        elif kind == "home_set":
            other.append({"t": t, "kind": kind, "detail": f"home point set ({ev.get('source', 'unknown')})"})
        elif kind == "home_confirmed":
            other.append({"t": t, "kind": kind, "detail": f"home keyframe matched {ev.get('offset_m')} m from home "
                                                          f"({ev.get('inliers')} inliers)"})
        elif kind == "warmup_done":
            other.append({"t": t, "kind": kind, "detail": "baseline ready, detection active"})
        elif kind == "demo_inject":
            other.append({"t": t, "kind": kind, "detail": _injection_text(ev)})
        elif kind == "antenna":
            other.append({"t": t, "kind": kind, "detail": f"antenna {ev.get('status')} (was {ev.get('previous')})"})
        elif kind not in ("level", "state"):
            other.append({"t": t, "kind": kind, "detail": kind})

    meta = {"source": info.get("source"), "mode": info.get("mode"), "created_utc": info.get("created_utc"),
            "node_id": info.get("node_id"), "start_t_gnss": epochs[0]["t_gnss"] if epochs else None,
            "duration_s": timeline[-1]["t"] if timeline else 0, "epochs": len(epochs),
            "home_set": info.get("home") is not None, "state_source": STATE_SOURCE,
            "video": None, "video_fps": None, "reference_track": None}

    if video:
        video = Path(video)
        times = _frame_times(video, frames_csv, video_start)
        meta["video_fps"] = _transcode(video, times, session / "dashboard_video.webm")
        meta["video"] = "dashboard_video.webm"
        i = 0
        for row in timeline:
            target = t0 + row["t"]
            while i + 1 < len(times) and times[i + 1] <= target:
                i += 1
            row["frame"] = i if times and times[0] <= target else None

    return {"format": FORMAT, "meta": meta, "config": indicator_config(cfg), "timeline": timeline,
            "transitions": transitions, "detector_levels": levels, "keyframes": keyframes, "routes": routes,
            "events": other}


def write_dashboard_json(session: str | Path, cfg: dict, **kw) -> Path:
    data = export_dashboard(session, cfg, **kw)
    out = Path(session) / "dashboard.json"
    out.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    return out
