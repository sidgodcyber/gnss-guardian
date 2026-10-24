"""DetectionEngine: one Epoch in, one Verdict (JSON-safe dict) out, every fix.

    eng = DetectionEngine(cfg)
    v = eng.update(epoch)                 # features, jamming + spoofing score/level/reasons
    eng.observe_vision(mono, v_e, v_n, q) # Phase 2: camera velocity (on-board only)
    eng.release_spoofing()                # Phase 3: GNSS and the camera agreed, drop the spoof latch

GNSS is trusted only when the baseline is warm, the fix is valid and both detectors are CLEAR.
The baseline grows only from epochs that both detectors call clean.
"""
from __future__ import annotations

import math

from gnss_guardian.detect.features import FeatureExtractor
from gnss_guardian.detect.hysteresis import CLEAR, Hysteresis
from gnss_guardian.detect.jamming import LABEL as JAM_LABEL
from gnss_guardian.detect.jamming import JammingDetector
from gnss_guardian.detect.scoring import Score
from gnss_guardian.detect.spoofing import SpoofingDetector
from gnss_guardian.io.epochs import Epoch

SPOOF_LABEL = "GNSS spoofing suspected"
FEATURE_KEYS = ("n_visible", "n_tracked", "n_used", "cn0_mean", "cn0_std", "n_judged", "n_dropped", "n_lost",
                "tracked_drop_db",
                "drop_frac", "lost_frac", "mean_drop_db", "hdop", "hdop_base", "hdop_ratio", "fix_lost",
                "n_newcomers", "newcomer_frac", "jump_m", "implied_speed_mps", "sog_mps", "alt_rate_mps",
                "time_offset_s", "time_jump_s", "vision_diff_mps", "vision_drift_mps")


def _r(x, nd=3):
    if isinstance(x, float):
        return round(x, nd) if math.isfinite(x) else None
    return x


class DetectionEngine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        h = cfg["detect"]["hysteresis"]
        self.features = FeatureExtractor(cfg)
        self.jam = JammingDetector(cfg)
        self.spoof = SpoofingDetector(cfg)
        self.h_jam = Hysteresis(h, h["min_alarm_s"]["jamming"])
        self.h_spoof = Hysteresis(h, h["min_alarm_s"]["spoofing"])
        self.suspect_on = h["suspect_on"]
        self._vision: tuple | None = None

    @property
    def baseline(self):
        return self.features.baseline

    def observe_vision(self, mono: float, v_e: float, v_n: float, quality: float) -> None:
        """Latest camera velocity (east/north m/s) with its quality 0-1 (Phase 2)."""
        self._vision = (mono, v_e, v_n, quality)

    def release_spoofing(self) -> None:
        """The state machine saw GNSS and the camera agree for state.spoof_release.agree_s: end the
        spoofing alarm now instead of after min_alarm_s. A spoof still present re-alarms as usual."""
        self.h_spoof.release()

    def update(self, ep: Epoch) -> dict:
        f = self.features.compute(ep, self._vision)
        t = f["t"]
        b = self.baseline
        warming = not b.ready
        if warming:
            js, ss = Score(), Score()
            msg = (f"warming up: {b.clean_s:.0f}/{b.warmup_s} s of clean data" if ep.fix_valid
                   else "warming up: waiting for a fix")
            js.notes.append(msg)
            ss.notes.append(msg)
        else:
            js = self.jam.score(f)
            jammed = self.h_jam.level != CLEAR or js.value >= self.suspect_on
            ss = self.spoof.score(f, jammed=jammed)
        lj, cj = self.h_jam.update(js.value, t)
        ls, cs = self.h_spoof.update(ss.value, t)
        if warming:
            clean = ep.fix_valid
        else:
            clean = lj == CLEAR and ls == CLEAR and js.value < self.suspect_on and ss.value < self.suspect_on
        self.features.commit(ep, f, clean)
        trusted = (not warming) and ep.fix_valid and lj == CLEAR and ls == CLEAR
        labels = ([JAM_LABEL] if lj != CLEAR else []) + ([SPOOF_LABEL] if ls != CLEAR else [])
        return {
            "seq": ep.seq,
            "t_gnss": ep.t_gnss.isoformat().replace("+00:00", "Z") if ep.t_gnss else None,
            "t_host": ep.t_host.isoformat().replace("+00:00", "Z") if ep.t_host else None,
            "mono": _r(ep.mono, 3),
            "fix_valid": ep.fix_valid,
            "lat": _r(ep.lat, 7), "lon": _r(ep.lon, 7), "alt_m": _r(ep.alt_m, 1),
            "sog_mps": _r(ep.sog_mps, 2), "cog_deg": _r(ep.cog_deg, 1), "hdop": _r(ep.hdop, 2),
            "antenna": ep.antenna,
            "warming_up": warming,
            "clean": clean,
            "gnss_trusted": trusted,
            "labels": labels,
            "jamming": {"level": lj, "changed": cj, **js.as_dict()},
            "spoofing": {"level": ls, "changed": cs, **ss.as_dict()},
            "features": {k: _r(f.get(k)) for k in FEATURE_KEYS},
            "sats": {k: [_r(s.cn0, 1), _r(s.elev, 0), s.used] for k, s in sorted(ep.sats.items())},
        }
