"""Two display numbers per epoch, each with the reasons that lowered it. Display only: nothing in
detection, the state machine or guidance reads them back.

GNSS Trust (0-100), every epoch:
    score = min(cap, 100 - sum(penalties))
    penalties   JAMMING   trust.gnss.weights.jamming  x jamming score
                SPOOFING  trust.gnss.weights.spoofing x spoofing score
                AI        trust.gnss.weights.ai       x AI second-opinion score (when the AI is on)
                HDOP      trust.gnss.weights.hdop     x ramp(HDOP / baseline HDOP)
                NO_FIX    trust.gnss.no_fix (no valid fix)
    caps        per state (trust.gnss.state_caps: warm-up, SUSPECT, an alarm and the navigation that follows),
                LATCH (spoof latch: GNSS untrusted until it agrees with the camera), and UNTRUSTED
                (trust.gnss.untrusted_cap < 80) whenever GNSS is not trusted
    Invariant: >= 80 only when gnss_trusted.

Nav confidence (0-100), only while the position comes from the camera (fused source vision / vision_stale):
    penalties   QUALITY   trust.nav.quality_weight x (1 - vision quality)
                DRIFT     trust.nav.drift_pts_per_m x metres dead-reckoned since the last absolute fix
                          (the last trusted GNSS anchor or keyframe / home match), at most drift_max
                HEADING   trust.nav.unaligned (camera heading not yet aligned to the GNSS course)
    caps        STALE (camera track lost: position held, trust.nav.stale_cap)
"""
from __future__ import annotations

import math

from gnss_guardian.detect.scoring import ramp

CAMERA_SOURCES = ("vision", "vision_stale")


def _band(score: int, bands) -> str:
    return "high" if score >= bands[0] else "medium" if score >= bands[1] else "low"


def _result(penalties: list[dict], caps: list[dict], bands) -> dict:
    raw = 100.0 - sum(p["points"] for p in penalties)
    cap = min(caps, key=lambda c: c["value"]) if caps else None
    score = int(round(max(0.0, min(raw, cap["value"] if cap else 100.0))))
    if cap is not None and cap["value"] >= raw:
        cap = None                                   # the penalties already explain a lower number
    return {"score": score, "band": _band(score, bands), "penalties": [p for p in penalties if p["points"] >= 0.5],
            "cap": cap}


def gnss_trust(v: dict, cfg: dict) -> dict:
    c = cfg["trust"]["gnss"]
    w = c["weights"]
    f = v.get("features") or {}
    pen: list[dict] = []

    def add(code, points, reason):
        pen.append({"code": code, "points": round(points, 1), "reason": reason})
    js, ss = v["jamming"]["score"], v["spoofing"]["score"]
    if js > 0:
        add("JAMMING", w["jamming"] * js, f"jamming/blockage score {js:.2f} ({v['jamming']['level']})"
            + (f": {v['jamming']['reasons'][0]}" if v["jamming"]["reasons"] else ""))
    if ss > 0:
        add("SPOOFING", w["spoofing"] * ss, f"spoofing score {ss:.2f} ({v['spoofing']['level']})"
            + (f": {v['spoofing']['reasons'][0]}" if v["spoofing"]["reasons"] else ""))
    ai = v.get("ai") or {}
    if ai.get("score"):
        add("AI", w["ai"] * ai["score"], f"AI second opinion {ai['score']:.2f}"
            + (f": {', '.join(ai.get('reasons', [])[:3])}" if ai.get("reasons") else ""))
    h = ramp(f.get("hdop_ratio"), c["hdop_ratio"])
    if h > 0:
        add("HDOP", w["hdop"] * h, f"HDOP {f['hdop_ratio']:.1f}x baseline")
    if not v.get("fix_valid"):
        add("NO_FIX", c["no_fix"], "no valid GNSS fix")
    caps = []
    st = v.get("state")
    if st in c["state_caps"]:
        caps.append({"code": f"STATE_{st}", "value": c["state_caps"][st], "reason": {
            "WARMING_UP": "baseline still warming up", "SUSPECT": "a detector is at SUSPECT"}.get(
            st, f"alarm held ({v.get('cause') or st.lower()}): GNSS not used for navigation")})
    if v.get("trust_blocked"):
        caps.append({"code": "LATCH", "value": c["latch_cap"], "reason": v["trust_blocked"]})
    if not v.get("gnss_trusted"):
        caps.append({"code": "UNTRUSTED", "value": c["untrusted_cap"], "reason": "GNSS not trusted this epoch"})
    return _result(pen, caps, c["bands"])


class NavConfidence:
    """Nav confidence while navigating on the camera; remembers how far the camera has dead-reckoned
    since the last absolute fix (trusted GNSS or a keyframe/home match)."""

    def __init__(self, cfg: dict):
        self.c = cfg["trust"]["nav"]
        self._last_pos: tuple[float, float] | None = None
        self._since_fix_m = 0.0
        self._last_match_t: float | None = None

    def update(self, v: dict) -> dict | None:
        fu, vis = v.get("fused"), v.get("vision") or {}
        if not fu or fu.get("source") not in CAMERA_SOURCES:
            self._last_pos, self._since_fix_m = None, 0.0           # GNSS (or nothing) carries the position
            return None
        c = self.c
        pos = (fu["e"], fu["n"])
        m = vis.get("last_match") or vis.get("home_confirmed")
        if m is not None and m.get("t") is not None and m["t"] != self._last_match_t:
            self._last_match_t, self._since_fix_m = m["t"], 0.0      # keyframe / home match: an absolute fix
        elif self._last_pos is not None:
            self._since_fix_m += math.hypot(pos[0] - self._last_pos[0], pos[1] - self._last_pos[1])
        self._last_pos = pos
        pen = []
        q = vis.get("quality")
        if q is not None and q < 1:
            pen.append({"code": "QUALITY", "points": round(c["quality_weight"] * (1 - q), 1),
                        "reason": f"camera track quality {q:.2f}"})
        d = min(c["drift_max"], c["drift_pts_per_m"] * self._since_fix_m)
        if d > 0:
            pen.append({"code": "DRIFT", "points": round(d, 1),
                        "reason": f"{self._since_fix_m:.1f} m dead-reckoned since the last absolute fix"})
        if not vis.get("aligned"):
            pen.append({"code": "HEADING", "points": float(c["unaligned"]),
                        "reason": "camera heading not aligned to the GNSS course yet (compass guidance)"})
        caps = []
        if fu["source"] == "vision_stale":
            caps.append({"code": "STALE", "value": c["stale_cap"], "reason": "camera track lost: position held"})
        out = _result(pen, caps, c["bands"])
        out["since_fix_m"] = round(self._since_fix_m, 1)
        return out
