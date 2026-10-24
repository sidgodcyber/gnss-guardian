"""VisionNav: camera odometry + terrain map, anchored to trusted GNSS.

    nav = VisionNav(cfg)
    nav.on_frame(t, gray)                       # every camera frame
    nav.set_home(t)                             # the pipeline just set home here (the rig stands on it)
    nav.on_gnss(t, verdict)                     # every GNSS epoch (after the detector's verdict)
    nav.velocity()                              # -> DetectionEngine.observe_vision(...)
    nav.displacement(t0, t1)                    # -> the detector's camera-vs-GNSS displacement check

Position (local east/north metres from home):
  vision = GNSS fix at t_a + camera displacement since t_a + keyframe corrections,
  where t_a trails the newest trusted fix by anchor_delay_s and stops moving the moment GNSS is
  not trusted. A spoof caught within anchor_delay_s therefore never becomes the anchor, and during
  denial the track is "from the last trusted GNSS fix", as Phase 3's state machine needs.
  When home is set automatically the anchor is home itself (0, 0): the rig stands there, whatever
  the newest fix says. While the camera has moved less than vision.stationary_m since the anchor,
  the anchor is kept: GNSS wander under a still camera does not move the vision position.

Home keyframe: taken on the first frame after home is set that has keyframes.home_min_features,
retried every frame while the camera stays within keyframes.home_confirm_m of the spot (home_kf
says "waiting" / "stored" / "missing" and why). A strict match against it (keyframes.home_match),
tried on every match cycle at any distance, resets the vision position and heading to the match.

Thread-safe: live mode calls on_frame from the camera thread and on_gnss/velocity from the serial loop.
"""
from __future__ import annotations

import math
import threading
from collections import deque
from pathlib import Path

import cv2

from gnss_guardian.vision.odometry import VisualOdometry, wrap180
from gnss_guardian.vision.terrain import TerrainMap


class VisionNav:
    def __init__(self, cfg: dict, size: tuple[int, int] | None = None, keyframe_dir: str | Path | None = None):
        self.cfg = cfg
        self.v, self.k = cfg["vision"], cfg["keyframes"]
        self.vo = VisualOdometry(cfg, size)
        self.map = TerrainMap(cfg, self.vo.size, self.vo.mpp)
        self.kf_dir = Path(keyframe_dir) if keyframe_dir else None
        self._lock = threading.Lock()
        self._gnss: deque = deque(maxlen=600)       # (t, e, n) of trusted epochs
        self._trusted_since: float | None = None
        self._anchor: tuple[float, float, float, float, float] | None = None   # t_a, gnss e/n, vo e/n at t_a
        self._corr = (0.0, 0.0)                     # keyframe corrections since the anchor
        self._trusted = False
        self._home_ready = False
        self._want_home_kf = False
        self._home_cam: tuple[float, float] | None = None    # odometry position where home was set
        self._home_given = False                    # home from the command line: the rig may be elsewhere
        self._best_features = 0
        self._frames = 0
        self._last_gnss_t: float | None = None
        self._courses: deque = deque(maxlen=3)      # (t, course) of recent moving trusted epochs
        self.last_match = None
        self.home_match = None
        self.home_kf: dict = {"status": "waiting", "detail": "home not set yet"}
        self.new_keyframes: list = []               # drained by the pipeline for events / thumbnails
        self.events: list[tuple[str, dict]] = []    # (kind, data) for the session log, drained by the pipeline

    # ---------------------------------------------------------------- inputs
    def on_frame(self, t: float, gray) -> dict:
        with self._lock:
            self._frames += 1
            st = self.vo.step(t, gray)
            pos = self._position()
            if self._want_home_kf:
                self._check_home_spot(pos)
            # keyframes need world positions: before the heading is aligned to the GNSS course the
            # track runs in an arbitrary direction (only the home keyframe, taken standing, is safe;
            # it is tried on every frame, its own feature count decides)
            if pos is not None and self._trusted and \
                    (self._want_home_kf or (self.vo.aligned and st["quality"] >= self.v["low_quality"])):
                home = self._want_home_kf
                kf = self.map.maybe_add(t, gray, pos[0], pos[1], self.vo.heading, home=home)
                if kf:
                    self._want_home_kf = False
                    self.new_keyframes.append(kf)
                    if kf.home:
                        self.home_kf = {"status": "stored", "features": len(kf.pts), "t": round(t, 3),
                                        "detail": f"home keyframe stored ({len(kf.pts)} features)"}
                    if self.kf_dir:
                        self.kf_dir.mkdir(parents=True, exist_ok=True)
                        cv2.imwrite(str(self.kf_dir / f"kf_{kf.id:04d}{'_home' if kf.home else ''}.jpg"), kf.thumb)
                elif home:
                    self._best_features = max(self._best_features, self.map.last_features)
                    self.home_kf = {"status": "waiting", "features": self.map.last_features,
                                    "need": self.k["home_min_features"],
                                    "detail": f"home keyframe waiting: {self.map.last_features} ORB features, "
                                              f"need {self.k['home_min_features']} (more texture under the camera?)"}
            elif pos is not None and not self._trusted and self._frames % self.k["match_every_n_frames"] == 0:
                self._correct(t, gray, pos)
            return st

    def _check_home_spot(self, pos) -> None:
        """Give up on the home keyframe once the camera has left the spot where home was set."""
        moved = math.hypot(self.vo.e - self._home_cam[0], self.vo.n - self._home_cam[1]) if self._home_cam else 0.0
        far = self._home_given and pos is not None and math.hypot(*pos) > self.k["home_confirm_m"]
        if moved > self.k["home_confirm_m"] or far:
            self._want_home_kf = False
            why = (f"camera moved {moved:.1f} m" if not far else f"rig {math.hypot(*pos):.1f} m from the given home")
            self.home_kf = {"status": "missing", "features": self._best_features,
                            "detail": f"NO home keyframe: {why} before a frame had {self.k['home_min_features']} "
                                      f"features (best {self._best_features}); HOME_REACHED falls back to the radius"}
            self.events.append(("home_keyframe_missing", dict(self.home_kf)))

    def _correct(self, t, gray, pos) -> None:
        m, h = self.map.match(gray, pos[0], pos[1])
        self.last_match = None
        if h is not None:                           # strict home match: we are where home was set
            jump = math.hypot(h.e - pos[0], h.n - pos[1])
            self._corr = (self._corr[0] + h.e - pos[0], self._corr[1] + h.n - pos[1])
            self.vo.rotate(wrap180(h.heading - self.vo.heading))
            self.last_match = {"t": round(t, 3), "kf": h.kf_id, "inliers": h.inliers, "correction_m": round(jump, 2),
                               "home": True}
            self.home_match = {"t": round(t, 3), "offset_m": round(h.offset_m, 2), "inliers": h.inliers,
                               "inlier_ratio": h.inlier_ratio, "reset_m": round(jump, 2)}
            return
        if m is None:
            return
        jump = math.hypot(m.e - pos[0], m.n - pos[1])
        if jump > self.k["max_correction_m"]:
            return
        g = self.k["correction_gain"]
        self._corr = (self._corr[0] + g * (m.e - pos[0]), self._corr[1] + g * (m.n - pos[1]))
        self.vo.rotate(g * wrap180(m.heading - self.vo.heading))
        self.last_match = {"t": round(t, 3), "kf": m.kf_id, "inliers": m.inliers, "correction_m": round(g * jump, 2),
                           "home": m.home}

    def set_home(self, t: float) -> None:
        """Home was just set from the fixes of the last home.set_after_s while the rig stood still:
        anchor the vision track at home (0, 0) now, and take the home keyframe here."""
        with self._lock:
            cur = self.vo.at(t) or (self.vo.e, self.vo.n)
            self._anchor = (t, 0.0, 0.0, cur[0], cur[1])
            self._corr = (0.0, 0.0)
            self._home_ready, self._home_given = True, False
            self._start_home_kf()

    def _start_home_kf(self) -> None:
        self._want_home_kf, self._home_cam, self._best_features = True, (self.vo.e, self.vo.n), 0
        self.home_kf = {"status": "waiting", "detail": "home keyframe: waiting for a good frame"}

    def on_gnss(self, t: float, v: dict) -> None:
        """One detector verdict (with enu_m relative to home once home is set)."""
        with self._lock:
            dt = (t - self._last_gnss_t) if self._last_gnss_t is not None else 0.0
            self._last_gnss_t = t
            trusted = bool(v.get("gnss_trusted")) and v.get("enu_m") is not None
            if trusted and not self._home_ready:    # home given on the command line (or set_home not used)
                self._home_ready, self._home_given = True, True
                self._start_home_kf()
            if trusted:
                if self._trusted_since is None:
                    self._trusted_since = t
                self._gnss.append((t, v["enu_m"][0], v["enu_m"][1]))
                if v.get("cog_deg") is not None and (v.get("sog_mps") or 0) >= self.v["align_min_speed_mps"]:
                    self._courses.append((t, v["cog_deg"]))
                    if self._straight(t):
                        was = self.vo.aligned
                        corr = self.vo.align_to_course(v["cog_deg"], dt)
                        if corr is not None and not was:
                            # keyframes stored before the first alignment (the home keyframe, taken while
                            # standing) carry headings in the unaligned frame: turn them with the snap
                            for kf in self.map.keyframes:
                                kf.heading = (kf.heading + corr) % 360.0
                else:
                    self._courses.clear()
                self._reanchor(t)                    # after alignment: the anchor uses the rotated history
            else:
                self._trusted_since = None
            self._trusted = trusted

    def _straight(self, t: float) -> bool:
        """Both the GNSS course and the camera heading steady over heading_align_window_s: during a
        turn the averaged vision track lags the GNSS course, and aligning then would bend the heading."""
        w, lim = self.v["heading_align_window_s"], self.v["heading_align_max_turn_deg"]
        recent = [c for tc, c in self._courses if tc >= t - w - 0.5]
        if len(recent) < 2 or max(abs(wrap180(c - recent[-1])) for c in recent) > lim:
            return False
        return self.vo.heading_change(w) <= lim

    def _reanchor(self, t: float) -> None:
        """Move the anchor to the newest trusted fix at least anchor_delay_s old (only within the
        current trusted stretch), unless the camera has not moved since the anchor. If there is none
        yet, keep the anchor but re-read its odometry coordinates: a heading alignment may just have
        rotated the odometry history."""
        cutoff = t - self.v["anchor_delay_s"]
        cand = [g for g in self._gnss if self._trusted_since <= g[0] <= cutoff]
        if cand and self._anchor is not None and \
                math.hypot(self.vo.e - self._anchor[3], self.vo.n - self._anchor[4]) < self.v["stationary_m"]:
            return                                   # standing still: GNSS wander does not move us
        if not cand:
            if self._anchor is None and self._gnss:          # start-up: anchor to the first fix
                cand = [self._gnss[0]]
            elif self._anchor is not None:
                cand = [self._anchor[:3]]
            else:
                return
        ta, ge, gn = cand[-1]
        vo = self.vo.at(ta)
        if vo is None:
            return
        self._anchor = (ta, ge, gn, vo[0], vo[1])
        self._corr = (0.0, 0.0)

    # ---------------------------------------------------------------- outputs
    def _position(self, t: float | None = None) -> tuple[float, float] | None:
        """Vision position now (latest frame), or at time t if it is inside the odometry history."""
        if self._anchor is None:
            return None
        _, ge, gn, ve0, vn0 = self._anchor
        cur = self.vo.at(t) if t is not None else None
        e, n = cur if cur is not None else (self.vo.e, self.vo.n)
        return ge + e - ve0 + self._corr[0], gn + n - vn0 + self._corr[1]

    def position_at(self, t: float) -> tuple[float, float] | None:
        with self._lock:
            return self._position(t)

    def velocity(self) -> tuple[float, float, float, float] | None:
        """(t, v_east, v_north, quality) for DetectionEngine.observe_vision; None until the heading is
        aligned to GNSS (an unaligned heading would make every comparison a false disagreement)."""
        with self._lock:
            if not self.vo.aligned:
                return None
            return self.vo.velocity()

    def displacement(self, t0: float, t1: float) -> tuple[float, float, bool, float] | None:
        """(d_east, d_north, aligned, mean quality) of the camera between t0 and t1, in the current
        (rotated) odometry frame; None outside the history. Unaligned, only the length is meaningful."""
        with self._lock:
            a, b = self.vo.at(t0), self.vo.at(t1)
            if a is None or b is None:
                return None
            return b[0] - a[0], b[1] - a[1], self.vo.aligned, self.vo.mean_quality(t0, t1)

    def breadcrumbs(self) -> list[tuple[int, float, float, bool]]:
        """(id, e, n, home) of the stored keyframes in storage order: the path walked while GNSS was
        trusted, which the return-home guidance reverses."""
        with self._lock:
            return [(k.id, k.e, k.n, k.home) for k in self.map.keyframes]

    def state(self, at: float | None = None) -> dict:
        """Vision state; e/n at time ``at`` (e.g. the GNSS epoch, so the two can be compared) when
        given, else at the latest frame."""
        with self._lock:
            t = self.vo._last_t
            pos = self._position(at) if t is not None else None
            since = None
            if self._anchor is not None:
                a = self.vo.at(self._anchor[0])
                since = None if a is None else math.hypot(self.vo.e - a[0], self.vo.n - a[1])
            vel = self.vo.velocity()
            home_d = math.hypot(*pos) if pos else None
            return {"t": None if t is None else round(t, 3),
                    "e": None if pos is None else round(pos[0], 2), "n": None if pos is None else round(pos[1], 2),
                    "heading": round(self.vo.heading, 1), "aligned": self.vo.aligned,
                    "mode": "gnss_anchored" if self._trusted else ("dead_reckoning" if pos else "waiting_for_home"),
                    "quality": round(self.vo.last["quality"], 2), "n_inliers": self.vo.last["n_inliers"],
                    "flag": self.vo.last["flag"],
                    "v_e": None if vel is None else round(vel[1], 2), "v_n": None if vel is None else round(vel[2], 2),
                    "since_anchor_m": None if since is None else round(since, 1),
                    "keyframes": len(self.map.keyframes), "home_keyframe": self.map.home is not None,
                    "home_kf": dict(self.home_kf),
                    "last_match": self.last_match, "home_dist_m": None if home_d is None else round(home_d, 2),
                    "home_confirmed": self.home_match}
