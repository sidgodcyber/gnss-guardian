"""Phase 3: navigation state, fused position and return-home guidance, one step per GNSS epoch.

    sm = StateMachine(cfg, camera=True)
    v["gnss_trusted"] &= not sm.gnss_blocked        # before the epoch reaches HomeSetter / VisionNav
    out = sm.update(t, v, vision_state, breadcrumbs) # after DetectionEngine and VisionNav

States (thresholds and timers in config ``state``):

  WARMING_UP    detector baseline not ready
  GNSS_OK       both detectors CLEAR
  SUSPECT       a detector at SUSPECT (GNSS not trusted: the camera track carries the position)
  JAMMED        jamming ALARM               the alarm; from here on the state leaves only through
  SPOOFED       spoofing ALARM              the release rule below
  VISION_NAV    the camera dead-reckons from the last trusted fix (heading aligned, track not lost)
  RETURN_HOME   VISION_NAV with a healthy track for return_home_after_s: guidance to home is active
  HOME_REACHED  camera position within home_radius_m of home, confirmed by a home keyframe match
                no older than home_match_max_age_s when a home keyframe exists

Release back to GNSS_OK:
  jamming   the detectors are CLEAR again (GNSS trusted)
  spoofing  with a camera: GNSS and the camera agree (position, velocity, quiet spoofing score, jamming
            CLEAR, fix valid) for spoof_release.agree_s in a row; the detector's spoof latch is then
            released too. Until then GNSS is not trusted even if the detector clears by itself
            (gnss_blocked), so a spoofer that turns quiet cannot become the anchor. Without a camera
            there is nothing to compare: the detector's own latch (min_alarm_s) releases it.
A spoofing ALARM during a jamming alarm turns the cause to spoofing (JAMMED -> SPOOFED).

Fused position (local east/north metres from home):
  gnss          GNSS trusted
  vision        camera dead reckoning anchored at the last trusted fix (VisionNav)
  vision_stale  the camera track has stopped (low texture: vision_lost); held, not extrapolated
  last_fix      no camera track: the last trusted GNSS position, held

Guidance is advisory only (the module never commands the aircraft): bearing (deg from true north)
and distance to home from the fused position, and the next waypoint on the reverse breadcrumb path
(the keyframes stored while GNSS was trusted, from the one nearest the current position back to home).
"""
from __future__ import annotations

import math

from gnss_guardian.detect.hysteresis import ALARM, CLEAR, SUSPECT as LEVEL_SUSPECT

WARMING_UP, GNSS_OK, SUSPECT = "WARMING_UP", "GNSS_OK", "SUSPECT"
JAMMED, SPOOFED, VISION_NAV = "JAMMED", "SPOOFED", "VISION_NAV"
RETURN_HOME, HOME_REACHED = "RETURN_HOME", "HOME_REACHED"
STATES = (WARMING_UP, GNSS_OK, SUSPECT, JAMMED, SPOOFED, VISION_NAV, RETURN_HOME, HOME_REACHED)
GUIDED = (RETURN_HOME, HOME_REACHED)


def bearing_deg(de: float, dn: float) -> float:
    """Direction of the vector (de, dn), degrees clockwise from true north."""
    return math.degrees(math.atan2(de, dn)) % 360.0


def _r(x, nd=2):
    return None if x is None else round(x, nd)


class Breadcrumbs:
    """Reverse path home: keyframe positions from the one nearest ``start`` back to the first, then
    home (0, 0). The home keyframe itself stands for home."""

    def __init__(self, crumbs: list[tuple[int, float, float, bool]], start: tuple[float, float], reached_m: float):
        path = [(i, e, n) for i, e, n, home in crumbs if not home]
        if path:
            k = min(range(len(path)), key=lambda j: math.hypot(path[j][1] - start[0], path[j][2] - start[1]))
            path = path[k::-1]
        self.route = [{"id": i, "e": round(e, 2), "n": round(n, 2)} for i, e, n in path]
        self.route.append({"id": "home", "e": 0.0, "n": 0.0})
        self.reached_m = reached_m
        self.next = 0

    def step(self, e: float, n: float) -> list[dict]:
        """Advance past the waypoints reached (or skipped: nearer to a later one); returns those passed."""
        r, before = self.route, self.next
        d = [math.hypot(w["e"] - e, w["n"] - n) for w in r[self.next:]]
        self.next += min(range(len(d)), key=d.__getitem__)
        while self.next < len(r) - 1 and math.hypot(r[self.next]["e"] - e, r[self.next]["n"] - n) <= self.reached_m:
            self.next += 1
        return r[before:self.next]

    def guidance(self, e: float, n: float) -> dict:
        w = self.route[self.next]
        rest = self.route[self.next:]
        path = math.hypot(w["e"] - e, w["n"] - n) + sum(
            math.hypot(b["e"] - a["e"], b["n"] - a["n"]) for a, b in zip(rest, rest[1:]))
        return {"waypoint": {"id": w["id"], "e": w["e"], "n": w["n"],
                             "bearing_deg": _r(bearing_deg(w["e"] - e, w["n"] - n), 1),
                             "distance_m": _r(math.hypot(w["e"] - e, w["n"] - n))},
                "waypoints_left": len(rest), "path_remaining_m": _r(path)}


class StateMachine:
    def __init__(self, cfg: dict, camera: bool):
        self.c = cfg["state"]
        self.rel = self.c["spoof_release"]
        self.reached_m = cfg["guidance"]["waypoint_reached_m"]
        self.camera = camera
        self.state: str | None = None
        self.cause: str | None = None                   # "jamming" | "spoofing" while an alarm is held
        self._healthy_since: float | None = None
        self._agree_since: float | None = None
        self._last_fix: tuple[float, float] | None = None
        self.crumbs: Breadcrumbs | None = None

    @property
    def gnss_blocked(self) -> bool:
        """GNSS must not be trusted on the next epoch whatever the detector says: a spoof alarm is
        held until GNSS and the camera agree."""
        return self.cause == "spoofing" and self.camera

    # ---------------------------------------------------------------- step
    def update(self, t: float, v: dict, vis: dict | None, crumbs: list | None = None) -> dict:
        """One epoch: ``v`` the detector verdict (gnss_trusted already gated, enu_m set), ``vis`` the
        VisionNav state at the epoch time (None without a camera), ``crumbs`` VisionNav.breadcrumbs()."""
        fused = self._fused(v, vis)
        healthy = self._healthy(vis)
        self._healthy_since = (self._healthy_since if self._healthy_since is not None else t) if healthy else None
        out = {"release_spoofing": False, "route": None, "passed": [], "latch": None, "escalated": False}
        prev, reasons = self.state, []
        new = self._next(t, v, vis, fused, healthy, reasons, out)
        if new in GUIDED and self.crumbs is None and fused is not None:
            self.crumbs = Breadcrumbs(crumbs or [], (fused["e"], fused["n"]), self.reached_m)
            out["route"] = self.crumbs.route
        if new not in GUIDED:
            self.crumbs = None
        guidance = None
        if self.crumbs is not None and fused is not None:
            out["passed"] = self.crumbs.step(fused["e"], fused["n"])
            guidance = {"advisory": True, "home_bearing_deg": fused["home_bearing_deg"],
                        "home_distance_m": fused["home_distance_m"], **self.crumbs.guidance(fused["e"], fused["n"])}
        self.state = new
        out.update(state=new, changed=new != prev, previous=prev, reasons=reasons, cause=self.cause,
                   fused=fused, guidance=guidance)
        return out

    def _next(self, t, v, vis, fused, healthy, reasons, out) -> str:
        if v.get("warming_up"):
            self.cause = None
            return WARMING_UP
        j, s = v["jamming"]["level"], v["spoofing"]["level"]
        if self.cause is None:
            if s == ALARM or j == ALARM:
                self.cause = "spoofing" if s == ALARM else "jamming"
                self._agree_since = None
                reasons += v[self.cause]["reasons"]
                return SPOOFED if s == ALARM else JAMMED
            active = [d for d in ("jamming", "spoofing") if v[d]["level"] == LEVEL_SUSPECT]
            if active:
                reasons += [r for d in active for r in v[d]["reasons"]]
                return SUSPECT
            if self.state not in (None, WARMING_UP, GNSS_OK):
                reasons.append("detectors CLEAR")
            return GNSS_OK

        st = self.state
        if self.cause == "jamming" and s == ALARM:     # a spoofer taking over a jammed sky
            self.cause, self._agree_since = "spoofing", None
            out["escalated"] = True
            if st == JAMMED:
                reasons += v["spoofing"]["reasons"]
                return SPOOFED
        if self._released(t, v, vis, reasons, out):
            self.cause, self._agree_since = None, None
            return GNSS_OK

        c = self.c
        if st in (JAMMED, SPOOFED):
            if healthy:
                reasons.append(f"camera dead reckoning from the last trusted fix "
                               f"({vis.get('since_anchor_m') or 0:.1f} m travelled since)")
                return VISION_NAV
        elif st == VISION_NAV:
            if healthy and fused is not None and t - self._healthy_since >= c["return_home_after_s"]:
                reasons.append(f"camera track healthy for {t - self._healthy_since:.0f} s; home "
                               f"{fused['home_distance_m']:.1f} m at {fused['home_bearing_deg']:.0f} deg")
                return RETURN_HOME
        elif st == RETURN_HOME:
            if self._at_home(t, vis, fused, reasons):
                return HOME_REACHED
        elif st == HOME_REACHED:
            if fused is not None and fused["home_distance_m"] > c["home_leave_m"]:
                reasons.append(f"{fused['home_distance_m']:.1f} m from home (> {c['home_leave_m']} m): guiding again")
                return RETURN_HOME
        return st

    # ---------------------------------------------------------------- rules
    def _released(self, t, v, vis, reasons, out) -> bool:
        if self.cause == "jamming" or not self.camera:
            if v["gnss_trusted"]:
                reasons.append("detectors CLEAR: GNSS trusted again" if self.cause == "jamming" else
                               "spoofing alarm ended by the detector latch (no camera to compare with)")
                return True
            return False
        ok, detail = self._agreement(v, vis)
        if not ok:
            self._agree_since = None
        elif self._agree_since is None:
            self._agree_since = t
        held = 0.0 if self._agree_since is None else t - self._agree_since
        out["latch"] = {"agree_s": round(held, 1), "needed_s": self.rel["agree_s"], "agreeing": ok, "detail": detail}
        if ok and held >= self.rel["agree_s"] - 0.1:          # epoch times carry a few ms of jitter
            reasons.append(f"GNSS and camera agreed for {held:.0f} s ({detail}): spoof latch released")
            out["release_spoofing"] = True
            return True
        return False

    def _agreement(self, v, vis) -> tuple[bool, str]:
        r, enu = self.rel, v.get("enu_m")
        if not v.get("fix_valid") or enu is None:
            return False, "no GNSS fix"
        if not self._healthy(vis):
            return False, "no camera track to compare with"
        d = math.hypot(enu[0] - vis["e"], enu[1] - vis["n"])
        vd = (v.get("features") or {}).get("vision_diff_mps")
        if d > r["max_pos_diff_m"]:
            return False, f"GNSS {d:.1f} m from the camera position"
        if vd is None:
            return False, "no GNSS-vs-camera velocity yet"
        if vd > r["max_vel_diff_mps"]:
            return False, f"GNSS velocity {vd:.2f} m/s off the camera"
        if v["spoofing"]["score"] > r["max_score"]:
            return False, f"spoofing score {v['spoofing']['score']:.2f}"
        if v["jamming"]["level"] != CLEAR:
            return False, f"jamming {v['jamming']['level']}"
        return True, f"position within {d:.1f} m, velocity within {vd:.2f} m/s"

    def _at_home(self, t, vis, fused, reasons) -> bool:
        c = self.c
        if fused is None or fused["source"] != "vision" or fused["home_distance_m"] > c["home_radius_m"]:
            return False
        if vis.get("home_keyframe"):
            hm = vis.get("home_confirmed")
            if hm is None or t - hm["t"] > c["home_match_max_age_s"]:
                return False
            reasons.append(f"{fused['home_distance_m']:.1f} m from home (radius {c['home_radius_m']} m); "
                           f"home keyframe matched {hm['offset_m']} m away ({hm['inliers']} inliers)")
        else:
            reasons.append(f"{fused['home_distance_m']:.1f} m from home (radius {c['home_radius_m']} m); "
                           f"no home keyframe to confirm with")
        return True

    @staticmethod
    def _healthy(vis) -> bool:
        return bool(vis) and vis.get("mode") == "dead_reckoning" and vis.get("e") is not None \
            and bool(vis.get("aligned")) and vis.get("flag") != "vision_lost"

    def _fused(self, v, vis) -> dict | None:
        enu = v.get("enu_m")
        if v["gnss_trusted"] and enu is not None:
            self._last_fix = (enu[0], enu[1])
            src, e, n = "gnss", enu[0], enu[1]
        elif vis and vis.get("mode") == "dead_reckoning" and vis.get("e") is not None:
            src = "vision_stale" if vis.get("flag") == "vision_lost" else "vision"
            e, n = vis["e"], vis["n"]
        elif self._last_fix is not None:
            src, (e, n) = "last_fix", self._last_fix
        else:
            return None
        return {"source": src, "e": _r(e), "n": _r(n), "home_distance_m": _r(math.hypot(e, n)),
                "home_bearing_deg": _r(bearing_deg(-e, -n), 1)}
