"""VisionNav: anchoring to trusted GNSS (and not to a spoof caught late), the keyframe terrain map,
drift correction from keyframes, home confirmation. Rendered ground, GNSS verdicts from the truth."""
import copy
import math

from conftest import synth_walk

from gnss_guardian.vision.nav import VisionNav
from gnss_guardian.vision.terrain import TerrainMap

Pose, Renderer, Trajectory, frames = synth_walk.Pose, synth_walk.Renderer, synth_walk.Trajectory, synth_walk.frames


def verdict(p, trusted=True, offset=(0.0, 0.0), moving=True, course=None, speed=1.2):
    return {"gnss_trusted": trusted, "enu_m": [p.east + offset[0], p.north + offset[1]],
            "cog_deg": course if course is not None else p.heading, "sog_mps": speed if moving else 0.02}


def test_anchor_survives_a_spoof_caught_late_and_keyframes(cfg, tmp_path):
    nav = VisionNav(cfg, keyframe_dir=tmp_path / "kf")
    traj = Trajectory(straight="12:1.2:90", still_s=3)
    last_g = -1
    for t, p, img in frames(traj, Renderer(), 14, 15):
        nav.on_frame(t, img)
        if int(t) != last_g:                             # 1 Hz GNSS
            last_g = int(t)
            moving = t >= 3
            if t < 8:
                nav.on_gnss(t, verdict(p, moving=moving, speed=1.2 if moving else 0.0))
            elif t < 10:                                 # 500 m jump, detector not yet in ALARM: still "trusted"
                nav.on_gnss(t, verdict(p, offset=(500.0, 0.0)))
            else:                                        # spoofing ALARM: GNSS ignored
                nav.on_gnss(t, verdict(p, trusted=False))
    st = nav.state()
    err = math.hypot(st["e"] - p.east, st["n"] - p.north)
    assert st["mode"] == "dead_reckoning" and err < 0.5, (st, p)
    kfs = nav.map.keyframes
    assert kfs[0].home and math.hypot(kfs[0].e, kfs[0].n) < 0.3
    assert 3 <= len(kfs) <= 6                                             # every 2 m over ~6 m of trusted travel
    gaps = [math.hypot(b.e - a.e, b.n - a.n) for a, b in zip(kfs[1:], kfs[2:])]
    assert all(g >= cfg["keyframes"]["spacing_m"] - 1e-6 for g in gaps)
    assert (tmp_path / "kf" / "kf_0000_home.jpg").exists()


def test_keyframes_correct_scale_drift_and_confirm_home(cfg):
    """Camera calibration 12 % off. Lap 1 with GNSS (keyframes), lap 2 GNSS-denied. On a closed
    loop a scale error cancels at the end, so the worst error over lap 2 is compared: dead
    reckoning alone is off by >1 m at the far corner; keyframe matches keep it well below, and
    the home keyframe is recognised on arrival."""
    bad = copy.deepcopy(cfg)
    bad["camera"]["focal_px"] = 635.5 * 1.12
    nomatch = copy.deepcopy(bad)
    nomatch["keyframes"]["match_every_n_frames"] = 10**9
    navs = {"keyframes": VisionNav(bad), "dead_reckoning": VisionNav(nomatch)}
    walk = "loop:12:6:1.2"
    lap = 36 / 1.2
    traj = Trajectory(walk=walk, still_s=3)
    last_g = -1
    worst = dict.fromkeys(navs, 0.0)
    for t, p, img in frames(traj, Renderer(), 3 + 2 * lap + 1, 15):
        g = int(t) != last_g
        last_g = int(t)
        for k, nav in navs.items():
            nav.on_frame(t, img)
            if g:
                denied = t >= 3 + lap - 1
                nav.on_gnss(t, verdict(p, trusted=not denied, moving=t >= 3, speed=1.2 if t >= 3 else 0.0))
                if denied:
                    st = nav.state()
                    worst[k] = max(worst[k], math.hypot(st["e"] - p.east, st["n"] - p.north))
    print(f"lap 2 GNSS-denied with 12 % scale error: worst error {worst}")
    assert worst["dead_reckoning"] > 1.0 and worst["keyframes"] < 0.5 * worst["dead_reckoning"]
    hm = navs["keyframes"].state()["home_confirmed"]
    assert hm is not None and hm["offset_m"] <= cfg["keyframes"]["home_confirm_m"]


def test_home_match_only_near_home(cfg):
    r = Renderer()
    tm = TerrainMap(cfg, (640, 480), 1 / 635.5)
    tm.maybe_add(0.0, r.frame(Pose(0, 0, 0, 30, 1.0)), 0.0, 0.0, 30.0, home=True)
    m = tm.match_home(r.frame(Pose(0, 0.2, 0.1, 75, 1.0)))               # 22 cm away, turned 45 deg
    assert m is not None and abs(m.offset_m - math.hypot(0.2, 0.1)) < 0.03
    assert abs(m.e - 0.2) < 0.03 and abs(m.n - 0.1) < 0.03 and abs((m.heading - 75 + 180) % 360 - 180) < 1
    assert tm.match_home(r.frame(Pose(0, 3.0, 0.0, 30, 1.0))) is None     # 3 m away: no overlap, no match
