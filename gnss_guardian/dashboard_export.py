"""Export session data to a format suitable for the dashboard replay viewer.

    Reads session/NAME/epochs.jsonl and events.jsonl, produces session/NAME/dashboard.json
    with a timeline of per-second GNSS state, detector levels, and vision track.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from gnss_guardian.session import read_jsonl


def export_dashboard(session_path: str | Path) -> dict:
    """Export session data to dashboard format.

    Returns dict with timeline (list of per-second snapshots) and metadata.
    Each timeline entry has: t_gnss, gnss (fix, sats_used, hdop, cn0_mean),
    detector states (jamming, spoofing), vision track (e, n in metres from home),
    keyframe count, and any state transitions at that second.
    """
    session_path = Path(session_path)

    # Read input files
    session_info = json.loads((session_path / "session.json").read_text())
    epochs = read_jsonl(session_path / "epochs.jsonl")
    events = read_jsonl(session_path / "events.jsonl")

    # Group events by type for quick lookup
    transitions_by_time = {}
    keyframes_by_time = {}

    for ev in events:
        t_gnss = ev.get("t_gnss")
        if ev["kind"] == "level":
            if t_gnss not in transitions_by_time:
                transitions_by_time[t_gnss] = []
            transitions_by_time[t_gnss].append({
                "detector": ev["detector"],
                "level": ev["level"],
                "score": ev["score"],
                "reasons": ev.get("reasons", [])
            })
        elif ev["kind"] == "keyframe":
            if t_gnss not in keyframes_by_time:
                keyframes_by_time[t_gnss] = []
            keyframes_by_time[t_gnss].append({
                "id": ev["id"],
                "e": ev.get("e"),
                "n": ev.get("n"),
                "features": ev.get("features", 0)
            })

    # Build timeline: one entry per second (group epochs that share the same second)
    timeline = {}
    for ep in epochs:
        t_gnss = ep["t_gnss"]

        # Round to second for timeline
        t_sec = t_gnss[:19] if "T" in t_gnss else t_gnss

        if t_sec not in timeline:
            timeline[t_sec] = {
                "t_gnss": t_sec,
                "gnss": {
                    "fix_valid": False,
                    "sats_used": 0,
                    "hdop": None,
                    "cn0_mean": 0,
                    "lat": None,
                    "lon": None,
                },
                "jamming_level": "CLEAR",
                "jamming_score": 0.0,
                "spoofing_level": "CLEAR",
                "spoofing_score": 0.0,
                "vision": {
                    "e": None,
                    "n": None,
                    "heading": None,
                    "keyframes": 0,
                },
                "transitions": [],
            }

        # Update with this epoch's data (last one wins for the second)
        entry = timeline[t_sec]
        entry["gnss"]["fix_valid"] = ep.get("fix_valid", False)
        entry["gnss"]["sats_used"] = ep["features"].get("n_used", 0)
        entry["gnss"]["hdop"] = ep.get("hdop")
        entry["gnss"]["cn0_mean"] = round(ep["features"].get("cn0_mean", 0), 1)
        entry["gnss"]["lat"] = ep.get("lat")
        entry["gnss"]["lon"] = ep.get("lon")

        # Detector states
        entry["jamming_level"] = ep["jamming"].get("level", "CLEAR")
        entry["jamming_score"] = ep["jamming"].get("score", 0.0)
        entry["spoofing_level"] = ep["spoofing"].get("level", "CLEAR")
        entry["spoofing_score"] = ep["spoofing"].get("score", 0.0)

        # Vision track (local ENU coordinates relative to home)
        if ep.get("enu_m"):
            entry["vision"]["e"] = ep["enu_m"][0]
            entry["vision"]["n"] = ep["enu_m"][1]

        vision = ep.get("vision", {})
        if vision.get("heading") is not None:
            entry["vision"]["heading"] = round(vision["heading"], 1)
        entry["vision"]["keyframes"] = vision.get("keyframes", 0)

        # Add any transitions that happened at this time
        if t_gnss in transitions_by_time:
            entry["transitions"] = transitions_by_time[t_gnss]

        # Add any keyframes
        if t_gnss in keyframes_by_time:
            entry["vision"]["new_keyframes"] = keyframes_by_time[t_gnss]

    # Convert to sorted list
    timeline_list = sorted(timeline.values(), key=lambda x: x["t_gnss"])

    return {
        "meta": {
            "source": session_info.get("source"),
            "mode": session_info.get("mode"),
            "created_utc": session_info.get("created_utc"),
            "home": session_info.get("home"),
            "node_id": session_info.get("node_id"),
        },
        "timeline": timeline_list,
        "summary": {
            "start_time": timeline_list[0]["t_gnss"] if timeline_list else None,
            "end_time": timeline_list[-1]["t_gnss"] if timeline_list else None,
            "duration_s": len(timeline_list),
            "max_hdop": max((e["gnss"]["hdop"] for e in timeline_list if e["gnss"]["hdop"]), default=None),
            "max_cn0_mean": max((e["gnss"]["cn0_mean"] for e in timeline_list), default=0),
        }
    }


def write_dashboard_json(session_path: str | Path) -> Path:
    """Export to dashboard.json and return its path."""
    session_path = Path(session_path)
    data = export_dashboard(session_path)
    out_path = session_path / "dashboard.json"
    out_path.write_text(json.dumps(data, indent=1))
    return out_path
