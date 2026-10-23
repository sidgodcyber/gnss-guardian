"""Frame-to-frame ground motion: Shi-Tomasi corners, pyramidal Lucas-Kanade with a forward-backward
check, then a RANSAC similarity fit (rotation + translation + uniform scale).

The similarity maps previous-frame pixels to current-frame pixels (full-resolution coordinates
even when flow runs on a downscaled image). Quality = min(1, inliers / full_quality_inliers) x
inlier ratio x (1 - median residual / ransac_px): few corners (bare concrete, water, darkness),
inconsistent motion (blur, moving shadows, a foot in view) or a loose fit all pull it down.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class FlowResult:
    ok: bool
    m: np.ndarray | None = None          # 2x3 similarity, previous -> current pixels
    n_tracked: int = 0
    n_inliers: int = 0
    inlier_ratio: float = 0.0
    residual_px: float = float("nan")
    quality: float = 0.0
    frames: int = 1                      # frames spanned (1 + failed frames skipped before this one)
    reset: bool = False                  # the reference was given up: motion over the gap unknown
    orb: bool = False                    # measured by the ORB fallback, not Lucas-Kanade


class FlowTracker:
    def __init__(self, cfg: dict):
        v = cfg["vision"]
        self.v = v
        self.scale = float(v["process_scale"])
        self.win = (v["lk_win_px"], v["lk_win_px"])
        self.lk = dict(winSize=self.win, maxLevel=v["lk_levels"],
                       criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, v["lk_iters"], 0.03))
        self.reset()

    def reset(self) -> None:
        self._prev: np.ndarray | None = None
        self._pts: np.ndarray | None = None
        self._n_ref = 0                 # tracks that survived their first step after the last detection
        self._fresh = False
        self._skipped = 0
        self._m_frame: np.ndarray | None = None      # last per-frame motion (3x3, processing pixels)
        self._orb = None

    def _prep(self, gray: np.ndarray) -> np.ndarray:
        if gray.ndim == 3:
            gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
        if self.scale != 1.0:
            gray = cv2.resize(gray, None, fx=self.scale, fy=self.scale, interpolation=cv2.INTER_AREA)
        return gray

    def _detect(self, img: np.ndarray) -> np.ndarray | None:
        v = self.v
        pts = cv2.goodFeaturesToTrack(img, maxCorners=v["max_corners"], qualityLevel=v["corner_quality"],
                                      minDistance=max(2, int(v["corner_min_dist_px"] * self.scale)), blockSize=7)
        self._fresh = True
        return pts

    def _redetect_below(self) -> float:
        """Re-detect when the tracks fall below redetect_below, or below 70 % of the tracks that
        survived their first step after the last detection (so ground where many corners are weak
        is not re-detected every frame)."""
        return min(self.v["redetect_below"], 0.7 * self._n_ref) if self._n_ref else self.v["redetect_below"]

    def step(self, gray: np.ndarray) -> FlowResult | None:
        """Motion from the reference frame to this one; None for the first frame.

        The reference is normally the previous frame. When a frame fails (few inliers, low
        quality: blur, a turn too fast for one frame, a foot in view), the reference is kept for up
        to max_skip_frames, so the next good frame measures the whole motion, rotation included,
        instead of losing it. After that the reference is reset (result.reset) and the motion of
        the gap is unknown. Lucas-Kanade starts from a constant-velocity prediction (the last
        per-frame motion repeated over the frames spanned), so bridged gaps and fast turns converge;
        if it still fails, ORB matching (rotation-invariant) is tried before giving up on the frame."""
        v = self.v
        cur = self._prep(gray)
        ref = self._prev
        if ref is None:
            self._prev, self._pts, self._skipped = cur, self._detect(cur), 0
            return None
        k = self._skipped + 1
        pred = np.linalg.matrix_power(self._m_frame, k) if self._m_frame is not None else None
        res, nxt = self._measure(ref, cur, pred, extra_level=k > 1)
        res.ok = res.ok and res.quality >= v["low_quality"]
        if not res.ok and v["orb_fallback"]:
            alt = self._measure_orb(ref, cur)        # rotation-invariant: a turn the prediction missed
            if alt is not None and alt.quality >= v["low_quality"]:
                res, nxt = alt, None
        res.frames = k
        if res.ok:
            self._m_frame = self._per_frame(res.m, k)
        if res.ok or self._skipped >= v["max_skip_frames"]:
            res.reset = not res.ok
            if res.reset:
                self._m_frame = None
            self._prev = cur
            self._pts = nxt if (res.ok and nxt is not None and len(nxt) >= self._redetect_below()) else self._detect(cur)
            self._skipped = 0
        else:
            self._skipped += 1
        return res

    def _measure_orb(self, ref: np.ndarray, cur: np.ndarray) -> FlowResult | None:
        """ORB matching between the reference and the current frame (only after Lucas-Kanade failed)."""
        if self._orb is None:
            self._orb = cv2.ORB_create(nfeatures=500)
            self._bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        ka, da = self._orb.detectAndCompute(ref, None)
        kb, db = self._orb.detectAndCompute(cur, None)
        v = self.v
        if da is None or db is None or len(ka) < v["min_inliers"] or len(kb) < v["min_inliers"]:
            return None
        mt = self._bf.match(da, db)
        if len(mt) < v["min_inliers"]:
            return None
        a = np.float32([ka[m.queryIdx].pt for m in mt])
        b = np.float32([kb[m.trainIdx].pt for m in mt])
        m, inl = cv2.estimateAffinePartial2D(a, b, method=cv2.RANSAC, ransacReprojThreshold=3.0 * self.scale,
                                             maxIters=2000, confidence=0.995)
        if m is None:
            return None
        inl = inl.ravel().astype(bool)
        ni = int(inl.sum())
        if ni < v["min_inliers"]:
            return None
        resid = float(np.median(np.linalg.norm(a[inl] @ m[:, :2].T + m[:, 2] - b[inl], axis=1))) / self.scale
        quality = min(1.0, ni / v["full_quality_inliers"]) * max(0.0, 1.0 - resid / (1.5 * v["ransac_px"]))
        if self.scale != 1.0:
            m = m.copy()
            m[:, 2] /= self.scale
        return FlowResult(ok=True, m=m, n_tracked=len(mt), n_inliers=ni, inlier_ratio=ni / len(mt),
                          residual_px=resid, quality=quality, orb=True)

    def _per_frame(self, m: np.ndarray, k: int) -> np.ndarray:
        """Per-frame similarity (processing pixels) from one spanning k frames: rotation / k,
        translation / k (small-angle), for the constant-velocity prediction."""
        s = self.scale
        ang = np.arctan2(m[1, 0], m[0, 0]) / k
        c, si = np.cos(ang), np.sin(ang)
        return np.array([[c, -si, m[0, 2] * s / k], [si, c, m[1, 2] * s / k], [0.0, 0.0, 1.0]])

    def _measure(self, ref: np.ndarray, cur: np.ndarray, pred: np.ndarray | None = None, extra_level: bool = False
                 ) -> tuple[FlowResult, np.ndarray | None]:
        v = self.v
        lk = dict(self.lk, maxLevel=self.lk["maxLevel"] + 1) if extra_level else self.lk   # bridged gaps move further
        p0 = self._pts
        if p0 is None or len(p0) < self._redetect_below() or (extra_level and not self._fresh):
            p0 = self._pts = self._detect(ref)       # bridging: fresh corners over the whole reference frame
        if p0 is None or len(p0) < v["min_inliers"]:
            return FlowResult(ok=False), None
        if pred is not None:                         # start LK where constant velocity puts each point
            guess = (p0.reshape(-1, 2) @ pred[:2, :2].T + pred[:2, 2]).astype(np.float32).reshape(-1, 1, 2)
            p1, st, _ = cv2.calcOpticalFlowPyrLK(ref, cur, p0, guess, flags=cv2.OPTFLOW_USE_INITIAL_FLOW, **lk)
        else:
            p1, st, _ = cv2.calcOpticalFlowPyrLK(ref, cur, p0, None, **lk)
        good = st.ravel() == 1
        if v["fb_check"]:
            pb, stb, _ = cv2.calcOpticalFlowPyrLK(cur, ref, p1, p0.copy(), flags=cv2.OPTFLOW_USE_INITIAL_FLOW, **lk)
            fb = np.linalg.norm((p0 - pb).reshape(-1, 2), axis=1)
            good &= (stb.ravel() == 1) & (fb < v["fb_max_px"] * max(self.scale, 0.5))
        a, b = p0[good].reshape(-1, 2), p1[good].reshape(-1, 2)
        n = len(a)
        if n < v["min_inliers"]:
            return FlowResult(ok=False, n_tracked=n), None
        m, inl = cv2.estimateAffinePartial2D(a, b, method=cv2.RANSAC, ransacReprojThreshold=v["ransac_px"] * self.scale,
                                             maxIters=2000, confidence=0.995)
        if m is None:
            return FlowResult(ok=False, n_tracked=n), None
        inl = inl.ravel().astype(bool)
        ni = int(inl.sum())
        proj = a[inl] @ m[:, :2].T + m[:, 2]
        resid = float(np.median(np.linalg.norm(proj - b[inl], axis=1))) / self.scale if ni else float("nan")
        ratio = ni / n
        if self._fresh:
            self._n_ref, self._fresh = ni, False
        fit = max(0.0, 1.0 - resid / v["ransac_px"]) if ni else 0.0
        quality = min(1.0, ni / v["full_quality_inliers"]) * ratio * fit
        if self.scale != 1.0:
            m = m.copy()
            m[:, 2] /= self.scale
        ok = ni >= v["min_inliers"]
        return (FlowResult(ok=ok, m=m, n_tracked=n, n_inliers=ni, inlier_ratio=ratio, residual_px=resid,
                           quality=quality if ok else 0.0), b[inl].reshape(-1, 1, 2))
