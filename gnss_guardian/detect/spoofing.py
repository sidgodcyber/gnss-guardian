"""Spoofing score: signals that are too clean, a sky that changed, or a fix that moves or ticks
in ways the rig cannot.

Indicators (combined with a noisy-OR, so one strong indicator alone raises the alarm):
  speed        implied speed between fixes (after a noise allowance) or reported speed above
               rig.max_speed_mps
  time         GNSS time jumped against the host monotonic clock
  altitude     altitude changed faster than the rig can climb
  uniform_cn0  C/N0 high and nearly identical across satellites (one transmitter, one antenna)
  newcomers    many tracked satellites the baseline window never saw
  vision       GNSS velocity disagrees with camera velocity: mean difference over 3 s (fast
               drag-off) or 10 s (slow drag-off)
  vision_pos   GNSS displacement disagrees with camera displacement over 30 s (a slow drift with a
               still camera; also before the camera heading is aligned, and when the receiver
               reports speed 0 while its position wanders)

A position jump within degraded_recent_s of jamming or a fix gap still alarms (a jam followed by
a spoof is a real attack); the reason then says "jump after degraded fix".

Limit: a slow drag-off below the rig's speed with natural-looking C/N0 and the same satellite
set is invisible to these GNSS-only indicators; only the vision cross-check can catch it.
"""
from __future__ import annotations

from gnss_guardian.detect.scoring import Score, noisy_or, ramp

KINEMATIC = ("speed", "altitude")
JUMP_AFTER_DEGRADED = "jump after degraded fix"


class SpoofingDetector:
    def __init__(self, cfg: dict):
        self.c = cfg["detect"]["spoofing"]
        self.vmax = cfg["rig"]["max_speed_mps"]

    def score(self, f: dict, jammed: bool = False) -> Score:
        c = self.c
        s = Score()
        speed = max(f["implied_speed_mps"] or 0.0, f["sog_mps"] or 0.0)
        uniform = 0.0
        if f["n_cn0"] >= c["uniform_min_sats"] and f["cn0_std"] is not None:
            uniform = ramp(f["cn0_std"], c["uniform_std_db"]) * ramp(f["cn0_mean"], c["uniform_mean_dbhz"])
        newcomers = 0.0
        if f["n_tracked"] >= c["newcomer_min_sats"]:
            newcomers = ramp(f["newcomer_frac"], c["newcomer_frac"])
        vision = 0.0
        if f.get("vision_quality", 0) >= c["vision_min_quality"]:
            vision = max(ramp(f.get("vision_diff_mps"), c["vision_diff_mps"]),
                         ramp(f.get("vision_drift_mps"), c["vision_drift_mps"]))
        vision_pos = 0.0
        if (f.get("vision_pos_quality") or 0) >= c["vision_min_quality"]:
            vision_pos = ramp(f.get("vision_pos_drift_m"), c["vision_pos_drift_m"])
        s.parts = {"speed": ramp(speed / self.vmax, c["speed_ramp_x"]),
                   "time": ramp(f["time_jump_s"], c["time_jump_s"]),
                   "altitude": ramp(f["alt_rate_mps"], c["alt_rate_mps"]),
                   "uniform_cn0": uniform, "newcomers": newcomers, "vision": vision, "vision_pos": vision_pos}
        if jammed and c["gate_kinematic_when_jammed"]:
            for k in KINEMATIC:
                if s.parts[k] > 0:
                    s.notes.append(f"{k} indicator ignored: fix degraded by jamming/blockage")
                s.parts[k] = 0.0
        s.value = noisy_or(s.parts, c["weights"])
        p = s.parts
        if p["speed"] > 0:
            if (f["implied_speed_mps"] or 0) >= (f["sog_mps"] or 0):
                s.reasons.append(f"position jumped {f['jump_m']:.0f} m: implied speed {f['implied_speed_mps']:.1f} m/s "
                                 f"(rig max {self.vmax:g})")
            else:
                s.reasons.append(f"reported speed {f['sog_mps']:.1f} m/s (rig max {self.vmax:g})")
            if f.get("after_degraded"):
                s.reasons.append(JUMP_AFTER_DEGRADED)
        if p["time"] > 0:
            s.reasons.append(f"GNSS time jumped {f['time_jump_s']:.1f} s against the host clock")
        if p["altitude"] > 0:
            s.reasons.append(f"altitude changing {f['alt_rate_mps']:.0f} m/s")
        if p["uniform_cn0"] > 0:
            s.reasons.append(f"C/N0 suspiciously uniform: std {f['cn0_std']:.1f} dB, mean {f['cn0_mean']:.1f} dB-Hz "
                             f"over {f['n_cn0']} satellites")
        if p["newcomers"] > 0:
            s.reasons.append(f"{f['n_newcomers']}/{f['n_tracked']} tracked satellites not seen in the baseline window")
        if p["vision"] > 0:
            short = ramp(f.get("vision_diff_mps"), c["vision_diff_mps"])
            if short >= ramp(f.get("vision_drift_mps"), c["vision_drift_mps"]):
                s.reasons.append(f"GNSS velocity differs from vision by {f['vision_diff_mps']:.1f} m/s "
                                 f"over {c['vision_window_s']} s")
            else:
                s.reasons.append(f"GNSS track drifting from the vision track: {f['vision_drift_mps']:.2f} m/s "
                                 f"over {c['vision_long_window_s']} s")
        if p["vision_pos"] > 0:
            how = "" if f["vision_pos_aligned"] else " (lengths only: camera heading not aligned yet)"
            s.reasons.append(f"GNSS moved {f['vision_pos_gnss_m']:.1f} m, camera {f['vision_pos_cam_m']:.1f} m over "
                             f"{c['vision_pos_window_s']} s: {f['vision_pos_drift_m']:.1f} m apart{how}")
        return s
