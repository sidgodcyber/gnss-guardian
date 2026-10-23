"""Per-epoch GNSS health features against a rolling baseline of clean epochs.

The baseline (per-satellite C/N0 median over the last ``window_s``, plus HDOP, fix and the
GNSS-minus-monotonic clock offset) is updated only with epochs the detectors call clean, and
nothing in it expires while they are not: an attack that lasts minutes cannot drag the
reference down to itself, and satellites that return after a jam are still known.
"""
from __future__ import annotations

import math
import statistics
from collections import deque

from gnss_guardian import geo
from gnss_guardian.io.epochs import Epoch


def epoch_time(ep: Epoch) -> float:
    """Seconds on the host clock if known, else GNSS time, else the sequence number."""
    if ep.mono is not None:
        return ep.mono
    if ep.t_gnss is not None:
        return ep.t_gnss.timestamp()
    return float(ep.seq)


class Baseline:
    def __init__(self, cfg: dict):
        b = cfg["detect"]["baseline"]
        self.window_s, self.min_samples, self.warmup_s = b["window_s"], b["min_samples"], b["warmup_s"]
        self.min_base_cn0 = b["min_base_cn0_dbhz"]
        self.sats: dict[str, deque] = {}
        self.hdop: deque = deque()
        self.fix: deque = deque()
        self.tracked: deque = deque()
        self.offset: deque = deque(maxlen=b["time_offset_samples"])
        self.clean_s = 0.0
        self._last_clean: float | None = None

    @property
    def ready(self) -> bool:
        return self.clean_s >= self.warmup_s

    def add(self, ep: Epoch, t: float, offset: float | None) -> None:
        if self._last_clean is not None:
            self.clean_s += min(max(t - self._last_clean, 0.0), 2.0)   # gaps do not count as clean time
        self._last_clean = t
        if ep.sats_new:
            for k, s in ep.tracked().items():
                self.sats.setdefault(k, deque()).append((t, s.cn0))
        self.fix.append((t, ep.fix_valid))
        self.tracked.append((t, len(ep.tracked())))
        if ep.hdop is not None:
            self.hdop.append((t, ep.hdop))
        if offset is not None:
            self.offset.append(offset)
        lo = t - self.window_s
        for k in list(self.sats):
            d = self.sats[k]
            while d and d[0][0] < lo:
                d.popleft()
            if not d:
                del self.sats[k]
        for d in (self.hdop, self.fix, self.tracked):
            while d and d[0][0] < lo:
                d.popleft()

    def sat_medians(self) -> dict[str, float]:
        """Per-satellite median C/N0 of satellites with enough samples and a usable level."""
        out = {}
        for k, d in self.sats.items():
            if len(d) >= self.min_samples:
                m = statistics.median(v for _, v in d)
                if m >= self.min_base_cn0:
                    out[k] = m
        return out

    def known(self) -> set[str]:
        return set(self.sats)

    def median(self, name: str) -> float | None:
        d = getattr(self, name)
        vals = [v for _, v in d] if name != "offset" else list(d)
        return statistics.median(vals) if vals else None

    def fix_fraction(self) -> float | None:
        return sum(v for _, v in self.fix) / len(self.fix) if self.fix else None


class FeatureExtractor:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        d = cfg["detect"]
        self.mask = d["elev_mask_deg"]
        self.drop_db = d["jamming"]["drop_db"]
        self.lost_cn0 = d["jamming"]["lost_cn0_dbhz"]
        self.slack_m = d["spoofing"]["jump_slack_m"]
        self.vision_max_age = d["spoofing"]["vision_max_age_s"]
        self.vision_window = d["spoofing"]["vision_window_s"]
        self._vdiff: deque = deque()                 # (t, d_east, d_north) GNSS minus vision velocity
        self.baseline = Baseline(cfg)
        self._prev_fix: tuple[float, float, float, float | None] | None = None   # t, lat, lon, alt

    def compute(self, ep: Epoch, vision: tuple | None = None) -> dict:
        t = epoch_time(ep)
        tracked = ep.tracked()
        cn0s = [s.cn0 for s in tracked.values() if not (s.elev < self.mask)]   # nan elevation kept
        f: dict = {"t": t, "n_visible": sum(1 for k in ep.sats if not k.startswith("S")),
                   "n_tracked": len(tracked),
                   "n_used": ep.sats_used if ep.sats_used is not None else sum(s.used for s in tracked.values()),
                   "cn0_mean": statistics.fmean(cn0s) if cn0s else None,
                   "cn0_std": statistics.pstdev(cn0s) if len(cn0s) >= 2 else None,
                   "n_cn0": len(cn0s), "fix_valid": ep.fix_valid, "hdop": ep.hdop, "sog_mps": ep.sog_mps,
                   "antenna": ep.antenna}
        # C/N0 against the per-satellite baseline
        base = self.baseline.sat_medians()
        drops, tdrops, lost = [], [], 0
        for k, b in base.items():
            s = tracked.get(k)
            if s is None:
                lost += 1
                drops.append(b - self.lost_cn0)
            else:
                drops.append(b - s.cn0)
                tdrops.append(b - s.cn0)
        n = len(base)
        f.update(n_judged=n, n_dropped=sum(x >= self.drop_db for x in drops), n_lost=lost,
                 n_judged_tracked=len(tdrops), tracked_drop_db=statistics.median(tdrops) if tdrops else None,
                 drop_frac=(sum(x >= self.drop_db for x in drops) / n) if n else None,
                 lost_frac=(lost / n) if n else None,
                 mean_drop_db=statistics.fmean(drops) if n else None)
        bh = self.baseline.median("hdop")
        f["hdop_base"] = bh
        f["hdop_ratio"] = (ep.hdop / bh) if (ep.hdop is not None and bh) else None
        ff = self.baseline.fix_fraction()
        f["fix_lost"] = (not ep.fix_valid) and ff is not None and ff >= 0.8
        # satellites the baseline window never saw
        known = self.baseline.known()
        new = [k for k in tracked if k not in known]
        f["n_newcomers"] = len(new)
        f["newcomer_frac"] = (len(new) / len(tracked)) if tracked and known else None
        # kinematics against the previous valid fix
        f["implied_speed_mps"] = f["alt_rate_mps"] = f["jump_m"] = None
        if ep.fix_valid and self._prev_fix is not None:
            pt, plat, plon, palt = self._prev_fix
            dt = t - pt
            if dt > 0:
                dist = geo.distance_m(plat, plon, ep.lat, ep.lon)
                f["jump_m"] = dist
                f["implied_speed_mps"] = max(0.0, dist - self.slack_m) / dt
                if palt is not None and ep.alt_m is not None:
                    f["alt_rate_mps"] = abs(ep.alt_m - palt) / max(dt, 1.0)
        # GNSS time against the host monotonic clock
        f["time_offset_s"] = f["time_jump_s"] = None
        if ep.mono is not None and ep.t_gnss is not None:
            off = ep.t_gnss.timestamp() - ep.mono
            f["time_offset_s"] = off
            bo = self.baseline.median("offset")
            if bo is not None:
                f["time_jump_s"] = abs(off - bo)
        # GNSS velocity against vision velocity (Phase 2 hook): the mean difference vector over
        # vision_window_s, so frame-to-frame flow noise averages out while a steady drag-off does not
        f["vision_diff_mps"] = None
        if vision is not None and ep.fix_valid and ep.sog_mps is not None and ep.mono is not None:
            vt, ve, vn, q = vision
            if ep.mono - vt <= self.vision_max_age and q is not None:
                ge, gn = geo.velocity_en(ep.sog_mps, ep.cog_deg or 0.0)
                self._vdiff.append((t, ge - ve, gn - vn))
                while self._vdiff and self._vdiff[0][0] <= t - self.vision_window:
                    self._vdiff.popleft()
                if len(self._vdiff) >= 2:
                    de = statistics.fmean(x for _, x, _ in self._vdiff)
                    dn = statistics.fmean(y for _, _, y in self._vdiff)
                    f["vision_diff_mps"] = math.hypot(de, dn)
                f["vision_quality"] = q
        return f

    def commit(self, ep: Epoch, feats: dict, clean: bool) -> None:
        """After the verdict: grow the baseline with clean epochs, remember the fix."""
        if clean:
            self.baseline.add(ep, feats["t"], feats["time_offset_s"])
        if ep.fix_valid:
            self._prev_fix = (feats["t"], ep.lat, ep.lon, ep.alt_m)
