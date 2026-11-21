"""Visual odometry: integrate frame-to-frame ground motion into a camera track (east/north metres
in the vision frame) and a heading.

Heading = sum of the similarity rotations, initialised (and slowly corrected while GNSS is trusted
and the rig moves) from the GNSS course, corrected by keyframe matches. Aligning the *track* angle
to the GNSS course, rather than copying the course into the heading, also learns how the camera is
turned on the pole (mount yaw). Low texture: the last velocity is kept for ``hold_s``, then the
track stops and frames are flagged ``vision_lost`` until one tracks again. A failed frame inside a
good stretch costs nothing: the flow keeps its reference frame and the next good frame measures the
whole motion.
"""
from __future__ import annotations

import bisect
import math
from collections import deque

from gnss_guardian.vision import geometry as geo
from gnss_guardian.vision.flow import FlowTracker


def wrap180(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0


class VisualOdometry:
    def __init__(self, cfg: dict, size: tuple[int, int] | None = None):
        v = cfg["vision"]
        self.v = v
        self.size = size or (cfg["camera"]["width"], cfg["camera"]["height"])
        self.mpp = geo.metres_per_px(cfg, self.size[0])
        self.flow = FlowTracker(cfg)
        self.heading = 0.0
        self.aligned = False
        self.e = self.n = 0.0                       # cumulative track, vision frame
        self.distance_m = 0.0
        self.hist: deque = deque()                  # (t, e, n, heading, quality)
        self.keep_s = max(v["velocity_window_s"], v["anchor_delay_s"],
                          cfg["detect"]["spoofing"]["vision_pos_window_s"]) + 5.0
        self._last_t: float | None = None
        self._t_ref: float | None = None           # time of the flow's reference frame
        self._last_good_t: float | None = None
        self._vbody = (0.0, 0.0)                    # last good body velocity (right, forward) m/s
        self.last = {"quality": 0.0, "n_inliers": 0, "n_tracked": 0, "flag": "start"}

    def step(self, t: float, gray) -> dict:
        res = self.flow.step(gray)
        flag = None
        if res is None:
            flag = "start"
            self._t_ref = t
        elif res.ok:
            r, f, dpsi = geo.camera_offset_from_match(res.m, self.size, self.mpp)
            psi_new = (self.heading - dpsi) % 360.0
            de, dn = geo.body_to_world(r, f, psi_new)     # reference camera position relative to the current one
            self.e -= de
            self.n -= dn
            self.distance_m += math.hypot(r, f)
            self.heading = psi_new
            dt = t - self._t_ref
            if dt > 0:
                self._vbody = (-r / dt, -f / dt)
            self._t_ref = self._last_good_t = t
        elif res.reset:
            # the flow gave up on its reference: dead-reckon the gap with the last velocity for up to hold_s
            gap = t - self._t_ref
            if self._last_good_t is not None and t - self._last_good_t <= self.v["hold_s"] and gap > 0:
                vr, vf = self._vbody
                de, dn = geo.body_to_world(vr * gap, vf * gap, self.heading)
                self.e += de
                self.n += dn
                flag = "low_texture"
            else:
                flag = "vision_lost"
            self._t_ref = t
        else:
            flag = "low_texture"            # skipped: the next good frame measures the motion since the reference
        if flag in ("start", "low_texture") and self.last["flag"] == "vision_lost":
            flag = "vision_lost"            # lost until a frame tracks again (no flicker back to "usable")
        q = res.quality if res is not None else 0.0
        self.hist.append((t, self.e, self.n, self.heading, q))
        while self.hist and self.hist[0][0] < t - self.keep_s:
            self.hist.popleft()
        self._last_t = t
        self.last = {"quality": q, "n_inliers": res.n_inliers if res else 0,
                     "n_tracked": res.n_tracked if res else 0, "flag": flag}
        return self.last

    def at(self, t: float) -> tuple[float, float] | None:
        """Track position at time t (linear between frames; None if outside the history)."""
        h = self.hist
        if not h or t < h[0][0] or t > h[-1][0] + 0.5:
            return None
        if t >= h[-1][0]:
            return h[-1][1], h[-1][2]
        k = bisect.bisect_right([x[0] for x in h], t)      # h[k-1].t <= t < h[k].t
        (t0, e0, n0, *_), (t1, e1, n1, *_) = h[k - 1], h[k]
        w = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
        return e0 + w * (e1 - e0), n0 + w * (n1 - n0)

    def mean_quality(self, t0: float, t1: float) -> float:
        q = [x[4] for x in self.hist if t0 <= x[0] <= t1]
        return sum(q) / len(q) if q else 0.0

    def velocity(self) -> tuple[float, float, float, float] | None:
        """(t, v_east, v_north, mean quality) over the last velocity_window_s; t is the centre of
        the window (the time the average velocity belongs to)."""
        h = self.hist
        if len(h) < 2:
            return None
        t1 = h[-1][0]
        win = [x for x in h if x[0] >= t1 - self.v["velocity_window_s"]]
        if len(win) < 2 or win[-1][0] - win[0][0] <= 0:
            return None
        dt = win[-1][0] - win[0][0]
        q = sum(x[4] for x in win) / len(win)
        return (win[0][0] + win[-1][0]) / 2, (win[-1][1] - win[0][1]) / dt, (win[-1][2] - win[0][2]) / dt, q

    def heading_change(self, window_s: float) -> float:
        """Largest heading change (deg) within the last window_s."""
        h = [x[3] for x in self.hist if x[0] >= self.hist[-1][0] - window_s] if self.hist else []
        return max((abs(wrap180(a - h[-1])) for a in h), default=0.0)

    def align_to_course(self, course_deg: float, dt: float) -> float | None:
        """While GNSS is trusted and the rig moves straight: rotate the heading so the vision track
        angle follows the GNSS course (snap on the first call, then time constant heading_align_tau_s).
        The track angle comes from the displacement over heading_align_window_s (longer than a pole
        sway period, whose tilt otherwise reads as sideways motion); the first snap waits for
        align_snap_min_m of travel. Returns the applied correction in degrees."""
        h = self.hist
        if len(h) < 2:
            return None
        w = [x for x in h if x[0] >= h[-1][0] - self.v["heading_align_window_s"]]
        span = w[-1][0] - w[0][0]
        de, dn = w[-1][1] - w[0][1], w[-1][2] - w[0][2]
        dist = math.hypot(de, dn)
        q = sum(x[4] for x in w) / len(w)
        if span <= 0 or dist / span < self.v["align_min_speed_mps"] or q < self.v["low_quality"]:
            return None
        if not self.aligned and dist < self.v["align_snap_min_m"]:
            return None
        err = wrap180(course_deg - math.degrees(math.atan2(de, dn)))
        corr = err if not self.aligned else err * min(1.0, dt / self.v["heading_align_tau_s"])
        self.rotate(corr)
        self.aligned = True
        return corr

    def rotate(self, deg: float) -> None:
        """Turn the heading by deg; the stored history is rotated about the current point so
        velocities stay consistent with the new heading."""
        if not deg:
            return
        self.heading = (self.heading + deg) % 360.0
        c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
        e0, n0 = self.e, self.n
        rot = deque()
        for t, e, n, hd, q in self.hist:
            de, dn = e - e0, n - n0
            rot.append((t, e0 + de * c + dn * s, n0 - de * s + dn * c, (hd + deg) % 360.0, q))
        self.hist = rot
