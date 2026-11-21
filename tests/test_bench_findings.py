"""The Pi 4 bench findings (indoors, rig stationary), reproduced on synthetic traces:

  1. jam on a still rig stayed JAMMED: the camera never counted as healthy because its heading only
     aligns to the GNSS course while walking. Now: JAMMED -> VISION_NAV -> RETURN_HOME -> HOME_REACHED.
  2. home was set from wandering indoor fixes (16-17 m off right after): the home gate now waits.
  3. no home keyframe: it was cancelled because the vision position (anchored on a wandering fix)
     was "far" from home. Now the anchor is home itself and the keyframe is retried until it has
     enough features.
  4. GNSS drifted 8 -> 27 m with a still camera, unflagged: the displacement check catches it.
  5. the snap back after a degraded fix alarmed (kept), now with the reason and toward/away.
  6. a SUSPECT from 2 of 4 satellites indoors: capped at SUSPECT while the fix holds.
Synthetic ground and simulated GNSS throughout.
"""
import copy
import math
from datetime import datetime

import numpy as np
import pytest
from conftest import START, fake_l86, fast_vision_cfg, synth_walk

from gnss_guardian.detect.jamming import JammingDetector
from gnss_guardian.home import HomeSetter
from gnss_guardian.run import replay
from gnss_guardian.session import read_jsonl
from gnss_guardian.state import HOME_REACHED, JAMMED, RETURN_HOME, VISION_NAV
from gnss_guardian.status import status_line
from gnss_guardian.vision.nav import VisionNav
from gnss_guardian.vision.terrain import TerrainMap

STILL_FPS, STILL_S = 10, 140


@pytest.fixture(scope="module")
def still_video(tmp_path_factory):
    """The bench: camera over textured ground, never moving."""
    out = tmp_path_factory.mktemp("still")
    synth_walk.main([str(out), "--still-s", str(STILL_S), "--duration-s", str(STILL_S), "--fps", str(STILL_FPS)])
    return out / "video.avi"


def bench(tmp_path, cfg, still_video, duration_s, *profile):
    a = fake_l86.build_parser().parse_args(["--write", str(tmp_path / "b.nmea"), "--start", START,
                                            "--duration-s", str(duration_s), *profile])
    fake_l86.write_file(a, fake_l86.load_cycles(fake_l86.FIXTURE), datetime.fromisoformat(START.replace("Z", "+00:00")))
    s = replay(tmp_path / "b.nmea", fast_vision_cfg(cfg), tmp_path / "s", video=still_video)
    return s, read_jsonl(tmp_path / "s" / "epochs.jsonl"), read_jsonl(tmp_path / "s" / "events.jsonl")


def test_jam_on_a_still_rig_reaches_home(tmp_path, cfg, still_video):
    s, eps, ev = bench(tmp_path, cfg, still_video, 70, "--jam", "sudden", "--jam-at", "40")
    chain = [x for x in s["states"] if x in (JAMMED, VISION_NAV, RETURN_HOME, HOME_REACHED)]
    assert chain == [JAMMED, VISION_NAV, RETURN_HOME, HOME_REACHED], s["states"]
    kf = [e for e in ev if e["kind"] == "keyframe" and e["home"]]
    assert len(kf) == 1 and kf[0]["features"] >= cfg["keyframes"]["home_min_features"]
    denied = [v for v in eps if v["seq"] >= 41]
    assert all(not v["vision"]["aligned"] for v in denied)                       # never walked: never aligned
    assert all(math.hypot(v["fused"]["e"], v["fused"]["n"]) < 0.3 for v in denied)
    home = next(v for v in eps if v["state"] == HOME_REACHED)
    assert home["seq"] <= 45 and home["vision"]["home_confirmed"]["inliers"] >= cfg["keyframes"]["home_match"]["min_inliers"]
    assert home["guidance"]["heading_aligned"] is False
    assert any(e["kind"] == "home_confirmed" for e in ev)
    line, _ = status_line(tmp_path / "s")
    assert "HOME_REACHED" in line and "home keyframe stored" in line


def test_home_waits_for_a_quality_window():
    cfg = {"home": {"set_after_s": 30, "max_hdop": 2.5, "min_sats_used": 6, "max_spread_m": 4.0}}
    h = HomeSetter(cfg)

    def fix(t, north_m, hdop=0.9, sats=9):
        return {"gnss_trusted": True, "lat": 29.0 + north_m / 111195, "lon": 79.0, "alt_m": 500.0,
                "hdop": hdop, "features": {"n_used": sats}, "t_gnss": f"t{t}"}
    for t in range(31):                                      # indoor wander: 0.5 m/s, 15 m over the window
        assert h.update(fix(t, 0.5 * t), t) is None
    assert h.waiting.startswith("home not set: fixes spread 7.5 m > 4 m")
    for t in range(31, 62):                                  # still now, but HDOP 4 and 5 satellites
        assert h.update(fix(t, 15.0, hdop=4.0, sats=5), t) is None
    assert "HDOP 4.0 > 2.5" in h.waiting and "5 satellites used < 6" in h.waiting
    assert h.update({"gnss_trusted": False}, 62) is None and "restarted" in h.waiting
    for t in range(63, 93):
        assert h.update(fix(t, 15.0), t) is None
    home = h.update(fix(93, 15.0), 93)
    assert home and abs(home["lat"] - (29.0 + 15.0 / 111195)) < 1e-6 and home["spread_m"] < 0.01
    assert home["n_fixes"] == 31 and h.waiting is None


def test_home_not_set_while_walking_and_status_says_why(tmp_path, cfg):
    from conftest import sim_file
    replay(sim_file(tmp_path / "w.nmea", duration_s=100), cfg, tmp_path / "w")       # walking from t = 0
    ev = read_jsonl(tmp_path / "w" / "events.jsonl")
    assert not any(e["kind"] == "home_set" for e in ev)
    assert any(e["kind"] == "home_waiting" and "fixes spread" in e["detail"] for e in ev)
    line, _ = status_line(tmp_path / "w")
    assert "home not set: fixes spread" in line


def test_gnss_wander_with_a_still_camera_is_flagged(tmp_path, cfg, still_video):
    """Indoor-style drift: 0.25 m/s, receiver speed stays 0. The vision position stays home."""
    s, eps, ev = bench(tmp_path, cfg, still_video, 100, "--spoof", "drift", "--drift-quiet", "--drift-mps", "0.25",
                       "--spoof-cn0", "natural", "--spoof-at", "40")
    first = next(v for v in eps if v["spoofing"]["level"] != "CLEAR")
    alarm = next(v for v in eps if v["spoofing"]["level"] == "ALARM")
    print(f"drift 0.25 m/s from 40 s: SUSPECT at {first['seq']} s, ALARM at {alarm['seq']} s")
    assert 40 < first["seq"] <= 68 and alarm["seq"] <= 72
    assert any("camera heading not aligned" in r for r in alarm["spoofing"]["reasons"])
    assert all(v["sog_mps"] < 0.1 for v in eps)                                    # the receiver says "still"
    before = [v for v in eps if 40 <= v["seq"] < first["seq"]]                      # drifting, still trusted
    assert before and max(math.hypot(*v["enu_m"]) for v in before) > 3
    assert all(math.hypot(v["vision"]["e"], v["vision"]["n"]) < 0.3 for v in before)   # the anchor held


def test_clean_still_gnss_with_wander_noise_no_false_alarm(tmp_path, cfg, still_video):
    s, eps, ev = bench(tmp_path, cfg, still_video, STILL_S - 2, "--pos-noise-m", "1.0")
    drift = [v["features"]["vision_pos_drift_m"] for v in eps if v["features"]["vision_pos_drift_m"] is not None]
    print(f"clean, 1 m wander noise: displacement check max {max(drift):.1f} m over {len(drift)} epochs")
    assert len(drift) > 60 and all(v["spoofing"]["level"] == "CLEAR" for v in eps)
    assert all(v["jamming"]["level"] == "CLEAR" for v in eps)


def test_snap_back_after_a_fix_gap_alarms_with_reason(tmp_path, cfg, still_video):
    """GNSS wanders 18 m away, the fix drops out, then snaps back to the true position."""
    s, eps, ev = bench(tmp_path, cfg, still_video, STILL_S - 2, "--spoof", "drift", "--drift-quiet", "--drift-mps", "0.3",
                       "--spoof-cn0", "natural", "--spoof-at", "35", "--recover-after", "60", "--inject-dropout", "88:4")
    snap = next(v for v in eps if v["seq"] >= 90 and (v["features"]["jump_m"] or 0) > 10)
    r = snap["spoofing"]["reasons"]
    print(f"snap at {snap['seq']} s: {r}")
    assert snap["spoofing"]["level"] == "ALARM" and snap["features"]["after_degraded"]
    assert any(x.startswith("jump after degraded fix: toward the camera position") for x in r)
    jc = snap["jump_check"]
    assert jc["toward_camera"] and jc["gnss_to_camera_before_m"] > 15 and jc["gnss_to_camera_after_m"] < 2
    assert "GNSS SPOOFING ALARM" in snap["labels"]                                  # an alarm, not "suspected"
    released = [v for v in eps if v["seq"] > snap["seq"] and v["state"] == "GNSS_OK"]
    assert released and "displacement within" in next(
        e for e in ev if e["kind"] == "state" and e["state"] == "GNSS_OK" and e["seq"] > snap["seq"])["reasons"][0]


def test_two_of_four_satellites_fading_is_only_suspect(cfg):
    jd = JammingDetector(cfg)
    f = {"n_judged": 4, "n_dropped": 2, "n_lost": 2, "n_judged_tracked": 2, "tracked_drop_db": 1.0,
         "drop_frac": 0.5, "lost_frac": 0.5, "mean_drop_db": 9.0, "hdop_ratio": 2.0, "fix_lost": False}
    s = jd.score(dict(f))
    h = cfg["detect"]["hysteresis"]
    assert h["suspect_on"] <= s.value < h["alarm_on"] and any("only 4 satellites judged" in n for n in s.notes)
    foil = jd.score(dict(f, fix_lost=True))                     # foil: the fix goes with them
    assert foil.value >= 0.9


# ---------------------------------------------------------------- home keyframe and strict home match
def test_home_keyframe_retried_until_enough_features_then_missing_if_moved(cfg):
    flat, tex = synth_walk.Renderer(flat=True), synth_walk.Renderer()
    nav = VisionNav(cfg)
    P = synth_walk.Pose
    nav.on_frame(0.0, flat.frame(P(0.0, 0, 0, 0, 1.0)))
    nav.on_gnss(0.0, {"gnss_trusted": True, "enu_m": [0.0, 0.0]})
    nav.set_home(0.0)
    for k in range(1, 6):                                    # bare ground at home: no keyframe yet, retried
        nav.on_frame(k / 10, flat.frame(P(k / 10, 0, 0, 0, 1.0)))
    assert nav.home_kf["status"] == "waiting" and nav.map.home is None and "need 120" in nav.home_kf["detail"]
    nav.on_frame(0.6, tex.frame(P(0.6, 0, 0, 0, 1.0)))
    assert nav.map.home is not None and nav.home_kf["status"] == "stored"
    nav2 = VisionNav(cfg)
    nav2.on_frame(0.0, tex.frame(P(0.0, 0, 0, 90, 1.0)))
    nav2.on_gnss(0.0, {"gnss_trusted": True, "enu_m": [0.0, 0.0]})
    nav2.set_home(0.0)
    nav2.map.k = dict(nav2.map.k, home_min_features=10**6)     # this ground never qualifies
    for k in range(1, 40):                                   # walk 2.6 m east before a good frame
        nav2.on_frame(k / 15, tex.frame(P(k / 15, 0.1 * k / 1.5, 0, 90, 1.0)))
    assert nav2.home_kf["status"] == "missing" and nav2.events[0][0] == "home_keyframe_missing"


def test_strict_home_match_rejects_repetitive_ground(cfg):
    """Ground that repeats every 128 px (~20 cm: tiles, crop rows, paving) with per-copy grain. A frame
    over another copy of the pattern must not confirm home. Loose thresholds (Lowe 0.75, 20 inliers,
    no inlier-ratio floor) confirm a false home on most seeds; the strict home match on none."""
    import cv2
    loose = copy.deepcopy(cfg)
    loose["keyframes"]["home_match"] = {"ratio": 0.75, "min_inliers": 20, "min_inlier_ratio": 0.0}
    period, fooled = 128, 0
    for seed in range(6):
        rng = np.random.default_rng(seed)
        tile = cv2.GaussianBlur((rng.random((period, period)) * 255).astype(np.float32), (0, 0), 1.5)
        big = np.tile(tile, (1400 // period, 1400 // period))
        big = np.clip(big + rng.normal(0, 6, big.shape), 0, 255).astype(np.uint8)
        other = big[period * 3:period * 3 + 480, period * 4:period * 4 + 640]      # 0.6 m away, same pattern
        for c in (cfg, loose):
            tm = TerrainMap(c, (640, 480), 1 / 635.5)
            assert tm.maybe_add(0.0, big[:480, :640], 0.0, 0.0, 0.0, home=True) is not None
            m = tm.match_home(other)
            if c is cfg:
                assert m is None, (seed, m)
            else:
                fooled += m is not None
    print(f"loose thresholds confirmed a false home on {fooled}/6 seeds; strict on 0/6")
    assert fooled >= 4
    r = synth_walk.Renderer()                                # ordinary ground: home is still recognised
    tm = TerrainMap(cfg, (640, 480), 1 / 635.5)
    tm.maybe_add(0.0, r.frame(synth_walk.Pose(0, 0, 0, 0, 1.0)), 0.0, 0.0, 0.0, home=True)
    m = tm.match_home(r.frame(synth_walk.Pose(0, 0.05, 0.03, 10, 1.0)))
    assert m is not None and m.inliers >= cfg["keyframes"]["home_match"]["min_inliers"] and m.offset_m < 0.1
