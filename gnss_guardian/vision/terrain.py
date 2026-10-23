"""Visual terrain map: ORB keyframes tagged with their GNSS position, stored while GNSS is trusted.

During vision navigation the current frame is matched against nearby keyframes. A RANSAC
similarity from the current image into the keyframe image gives the camera's offset in the
keyframe's body frame and the heading difference, hence an absolute position and heading that
removes accumulated drift. The home keyframe (taken where the home point was set) confirms
arrival home.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np

from gnss_guardian.vision import geometry as geo


@dataclass
class Keyframe:
    id: int
    t: float
    e: float
    n: float
    heading: float
    pts: np.ndarray
    desc: np.ndarray
    home: bool = False
    thumb: np.ndarray | None = field(default=None, repr=False)


@dataclass
class Match:
    kf_id: int
    e: float
    n: float
    heading: float
    inliers: int
    offset_m: float          # camera distance from the keyframe's camera position
    home: bool


class TerrainMap:
    def __init__(self, cfg: dict, size: tuple[int, int], mpp: float):
        self.k = cfg["keyframes"]
        self.size, self.mpp = size, mpp
        self.orb = cv2.ORB_create(nfeatures=self.k["orb_features"])
        self.bf = cv2.BFMatcher(cv2.NORM_HAMMING)
        self.keyframes: list[Keyframe] = []
        self._last_pos: tuple[float, float] | None = None

    def _features(self, gray):
        kp, desc = self.orb.detectAndCompute(gray, None)
        pts = np.float32([p.pt for p in kp]) if kp else np.zeros((0, 2), np.float32)
        return pts, desc

    @property
    def home(self) -> Keyframe | None:
        return next((k for k in self.keyframes if k.home), None)

    def maybe_add(self, t: float, gray, e: float, n: float, heading: float, home: bool = False) -> Keyframe | None:
        """Store a keyframe every spacing_m of travel (always when home=True)."""
        if not home and self._last_pos is not None and \
                math.hypot(e - self._last_pos[0], n - self._last_pos[1]) < self.k["spacing_m"]:
            return None
        pts, desc = self._features(gray)
        if desc is None or len(pts) < self.k["min_inliers"]:
            return None
        kf = Keyframe(len(self.keyframes), t, e, n, heading, pts, desc, home,
                      cv2.resize(gray, (160, 120), interpolation=cv2.INTER_AREA))
        self.keyframes.append(kf)
        self._last_pos = (e, n)
        return kf

    def _match_one(self, kf: Keyframe, pts, desc) -> Match | None:
        if desc is None or len(desc) < 2:
            return None
        pairs = self.bf.knnMatch(desc, kf.desc, k=2)
        good = [m for m, *rest in pairs if rest and m.distance < self.k["ratio"] * rest[0].distance]
        if len(good) < self.k["min_inliers"]:
            return None
        a = pts[[m.queryIdx for m in good]]
        b = kf.pts[[m.trainIdx for m in good]]
        m, inl = cv2.estimateAffinePartial2D(a, b, method=cv2.RANSAC, ransacReprojThreshold=3.0,
                                             maxIters=2000, confidence=0.995)
        if m is None or int(inl.sum()) < self.k["min_inliers"]:
            return None
        if not 0.85 < geo.similarity_scale(m) < 1.15:    # same height: a large zoom is a false match
            return None
        r, f, dpsi = geo.camera_offset_from_match(m, self.size, self.mpp)
        de, dn = geo.body_to_world(r, f, kf.heading)
        return Match(kf.id, kf.e + de, kf.n + dn, (kf.heading + dpsi) % 360.0, int(inl.sum()),
                     math.hypot(r, f), kf.home)

    def match(self, gray, e_est: float, n_est: float, max_candidates: int = 3) -> Match | None:
        """Best match among the keyframes within search_radius_m of the estimate."""
        near = sorted((math.hypot(k.e - e_est, k.n - n_est), k.id) for k in self.keyframes)
        near = [i for d, i in near if d <= self.k["search_radius_m"]][:max_candidates]
        if not near:
            return None
        pts, desc = self._features(gray)
        best = None
        for i in near:
            m = self._match_one(self.keyframes[i], pts, desc)
            if m and (best is None or m.inliers > best.inliers):
                best = m
        return best

    def match_home(self, gray) -> Match | None:
        h = self.home
        if h is None:
            return None
        pts, desc = self._features(gray)
        return self._match_one(h, pts, desc)
