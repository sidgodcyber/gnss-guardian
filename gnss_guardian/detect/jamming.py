"""GNSS denial score: broadband C/N0 collapse across most satellites at once.

The L86 reports no AGC or noise floor, so RF jamming, a foil-wrapped antenna, a building or
dense canopy all look the same from NMEA: the label is "GNSS denied: jamming or blockage".
For navigation the response is the same (stop trusting GNSS). What separates denial from a
few blocked satellites is breadth: most judged satellites drop together.
"""
from __future__ import annotations

from gnss_guardian.detect.scoring import Score, ramp, weighted_sum

LABEL = "GNSS denied: jamming or blockage"


class JammingDetector:
    def __init__(self, cfg: dict):
        self.c = cfg["detect"]["jamming"]
        self.min_sats = cfg["detect"]["baseline"]["min_base_sats"]

    def score(self, f: dict) -> Score:
        c = self.c
        s = Score()
        if f["n_judged"] < self.min_sats:
            s.notes.append(f"only {f['n_judged']} satellites with a baseline (need {self.min_sats}): not judged")
            return s
        s.parts = {"drop_frac": ramp(f["drop_frac"], c["drop_frac"]),
                   "mean_drop": ramp(f["mean_drop_db"], c["mean_drop_db"]),
                   "lost_frac": ramp(f["lost_frac"], c["lost_frac"]),
                   "hdop": ramp(f["hdop_ratio"], c["hdop_ratio"]),
                   "fix_lost": float(bool(f["fix_lost"]))}
        s.value = weighted_sum(s.parts, c["weights"])
        if f["fix_lost"] and (f["drop_frac"] or 0) >= c["fix_lost_with_drop"]:
            s.value = max(s.value, 0.9)
        elif f["n_judged"] < c["few_sats_judged"] and s.value > c["few_sats_cap"]:
            s.value = c["few_sats_cap"]         # 2 of 4 fading indoors: SUSPECT at most while the fix holds
            s.notes.append(f"only {f['n_judged']} satellites judged: score capped at {c['few_sats_cap']} "
                           f"(SUSPECT) while the fix holds")
        if (f["n_judged_tracked"] >= c["sky_change_min_tracked"] and f["tracked_drop_db"] is not None
                and f["tracked_drop_db"] < c["drop_db"] / 2):
            s.value *= c["sky_change_scale"]
            s.notes.append(f"satellites lost while the {f['n_judged_tracked']} still tracked keep their C/N0 "
                           f"(median drop {f['tracked_drop_db']:.1f} dB): sky change or partial blockage, not broadband")
        p = s.parts
        if p["drop_frac"] > 0:
            s.reasons.append(f"C/N0 down >= {c['drop_db']} dB on {f['n_dropped']}/{f['n_judged']} satellites")
        if p["mean_drop"] > 0:
            s.reasons.append(f"mean C/N0 drop {f['mean_drop_db']:.1f} dB")
        if p["lost_frac"] > 0:
            s.reasons.append(f"{f['n_lost']}/{f['n_judged']} satellites lost")
        if p["hdop"] > 0:
            s.reasons.append(f"HDOP {f['hdop_ratio']:.1f}x baseline")
        if f["fix_lost"]:
            if p["drop_frac"] > 0 or p["lost_frac"] > 0:
                s.reasons.append("fix lost")
            else:
                s.notes.append("fix lost but C/N0 normal on the judged satellites (not broadband)")
        return s
