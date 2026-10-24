"""Phase 3: navigation state, fused position, return-home guidance and the spoof latch.

Unit tests drive StateMachine with hand-made verdicts. The walk tests render an out-and-back walk
(stand 30 s, 12 m east and back at 1.2 m/s, stand) and replay it through the whole pipeline with a
jam or a spoof starting near the far end: the rig then walks back as a pilot following the guidance
would. All of it is synthetic ground and simulated GNSS.
"""
import csv
import math
from datetime import datetime

import pytest
from conftest import START, fake_l86, fast_vision_cfg, synth_walk

from gnss_guardian.run import replay
from gnss_guardian.session import read_jsonl
from gnss_guardian.state import (GNSS_OK, HOME_REACHED, JAMMED, RETURN_HOME, SPOOFED, VISION_NAV, WARMING_UP,
                                 Breadcrumbs, StateMachine, bearing_deg)
from gnss_guardian.vision.nav import VisionNav

CHAIN = [WARMING_UP, GNSS_OK, None, VISION_NAV, RETURN_HOME, HOME_REACHED, GNSS_OK]


def verdict(jam="CLEAR", spoof="CLEAR", enu=(0.0, 0.0), fix=True, warming=False, spoof_score=None, vdiff=0.0):
    return {"seq": 0, "t_gnss": None, "mono": None, "warming_up": warming, "fix_valid": fix,
            "gnss_trusted": jam == spoof == "CLEAR" and fix and not warming, "enu_m": list(enu) if enu else None,
            "jamming": {"level": jam, "score": 0.9 if jam == "ALARM" else 0.0,
                        "reasons": ["C/N0 down >= 6 dB on 12/12 satellites"] if jam != "CLEAR" else []},
            "spoofing": {"level": spoof, "score": spoof_score if spoof_score is not None else (0.95 if spoof == "ALARM" else 0.0),
                         "reasons": ["position jumped 500 m"] if spoof != "CLEAR" else []},
            "features": {"vision_diff_mps": vdiff}}


def vision(e, n, flag=None, aligned=True, home_kf=True, match_t=None, mode="dead_reckoning"):
    return {"mode": mode, "e": e, "n": n, "aligned": aligned, "flag": flag, "since_anchor_m": 3.0,
            "home_keyframe": home_kf,
            "home_confirmed": None if match_t is None else {"t": match_t, "offset_m": 0.2, "inliers": 150}}


def step(sm, t, v, vis=None, crumbs=None):
    """As the pipeline does: GNSS is not trusted while the state machine holds a spoof latch."""
    if sm.gnss_blocked:
        v["gnss_trusted"] = False
    return sm.update(t, v, vis, crumbs)


CRUMBS = [(0, 0.0, 0.0, True), (1, 2.0, 0.0, False), (2, 4.0, 0.0, False), (3, 6.0, 0.0, False), (4, 8.0, 0.0, False)]


def test_bearing_and_reverse_breadcrumb_path():
    assert [bearing_deg(*d) for d in ((0, 1), (1, 0), (0, -1), (-1, 0))] == [0, 90, 180, 270]
    b = Breadcrumbs(CRUMBS, (7.5, 0.0), reached_m=1.5)
    assert [w["id"] for w in b.route] == [4, 3, 2, 1, "home"]          # newest keyframe near us first, home last
    assert [w["id"] for w in b.step(7.5, 0.0)] == [4, 3]                # within 1.5 m of both
    g = b.guidance(7.5, 0.0)
    assert g["waypoint"]["id"] == 2 and g["waypoint"]["bearing_deg"] == 270 and g["waypoint"]["distance_m"] == 3.5
    assert g["waypoints_left"] == 3 and g["path_remaining_m"] == 7.5
    b = Breadcrumbs(CRUMBS, (8.0, 0.0), reached_m=1.5)
    assert [w["id"] for w in b.step(3.2, 0.0)] == [4, 3, 2, 1]          # skipped ahead to 2, then 1 is within reach
    assert Breadcrumbs([], (5.0, 5.0), 1.5).route == [{"id": "home", "e": 0.0, "n": 0.0}]


def test_jam_chain_fused_position_and_guidance(cfg):
    sm = StateMachine(cfg, camera=True)
    assert step(sm, 0, verdict(warming=True, enu=None))["state"] == WARMING_UP
    out = step(sm, 1, verdict(enu=(3.0, 0.0)), vision(3.0, 0.0, mode="gnss_anchored"))
    assert out["state"] == GNSS_OK and out["fused"]["source"] == "gnss" and out["guidance"] is None
    jam = verdict(jam="ALARM", fix=False, enu=None)
    seq = [step(sm, t, dict(jam), vision(8.0 - (t - 5), 0.0), CRUMBS) for t in range(5, 9)]
    assert [o["state"] for o in seq] == [JAMMED, VISION_NAV, RETURN_HOME, RETURN_HOME]
    assert seq[0]["cause"] == "jamming" and seq[0]["reasons"] == ["C/N0 down >= 6 dB on 12/12 satellites"]
    assert seq[1]["fused"] == {"source": "vision", "e": 7.0, "n": 0.0, "home_distance_m": 7.0, "home_bearing_deg": 270.0}
    assert [w["id"] for w in seq[2]["route"]] == [3, 2, 1, "home"] and seq[2]["guidance"]["advisory"] is True
    assert seq[3]["guidance"]["home_distance_m"] == 5.0 and seq[3]["guidance"]["waypoint"]["id"] == 1   # passed 3, 2
    # inside the radius, but the home keyframe has not matched (or only long ago): keep guiding
    assert step(sm, 9, dict(jam), vision(1.0, 0.0))["state"] == RETURN_HOME
    assert step(sm, 10, dict(jam), vision(1.0, 0.0, match_t=5.0))["state"] == RETURN_HOME
    out = step(sm, 11, dict(jam), vision(0.5, 0.0, match_t=10.8))
    assert out["state"] == HOME_REACHED and "home keyframe matched" in out["reasons"][0]
    assert step(sm, 12, dict(jam), vision(5.0, 0.0))["state"] == RETURN_HOME           # walked off: > home_leave_m
    out = step(sm, 13, verdict(enu=(4.0, 0.1)), vision(4.0, 0.0))
    assert out["state"] == GNSS_OK and out["reasons"] == ["detectors CLEAR: GNSS trusted again"]
    assert out["fused"]["source"] == "gnss" and out["guidance"] is None and out["cause"] is None


def test_home_reached_only_from_a_live_camera_position(cfg):
    sm = StateMachine(cfg, camera=True)
    step(sm, 0, verdict(enu=(6.0, 0.0)))
    jam = verdict(jam="ALARM", fix=False, enu=None)
    for t in range(1, 4):
        step(sm, t, dict(jam), vision(5.0, 0.0), CRUMBS)
    assert sm.state == RETURN_HOME
    out = step(sm, 4, dict(jam), vision(0.3, 0.0, flag="vision_lost", match_t=4.0))
    assert out["state"] == RETURN_HOME and out["fused"]["source"] == "vision_stale"
    out = step(sm, 5, dict(jam), vision(0.3, 0.0, home_kf=False))       # no home keyframe: the radius alone
    assert out["state"] == HOME_REACHED and "no home keyframe" in out["reasons"][0]


def test_spoof_latch_releases_only_after_agreement(cfg):
    sm = StateMachine(cfg, camera=True)
    need = cfg["state"]["spoof_release"]["agree_s"]
    step(sm, 0, verdict(enu=(2.0, 0.0)))
    out = step(sm, 1, verdict(spoof="ALARM", enu=(355.0, 355.0)), vision(2.0, 0.0))
    assert out["state"] == SPOOFED and sm.gnss_blocked
    # the detector clears by itself but GNSS is still 500 m off the camera: not trusted, not released
    for t in range(2, 8):
        out = step(sm, t, verdict(enu=(355.0, 355.0)), vision(2.0, 0.0))
        assert out["state"] != GNSS_OK and out["fused"]["source"] == "vision"
        assert not out["latch"]["agreeing"] and "500" in out["latch"]["detail"]
    # agreement for need - 1 s, broken by one velocity disagreement, then the full need
    for t in range(8, 8 + need):
        out = step(sm, t, verdict(enu=(2.3, 0.1)), vision(2.0, 0.0))
    assert out["state"] != GNSS_OK and out["latch"]["agree_s"] == need - 1
    out = step(sm, 8 + need, verdict(enu=(2.3, 0.1), vdiff=0.8), vision(2.0, 0.0))
    assert out["latch"]["agree_s"] == 0 and not out["latch"]["agreeing"]
    t0 = 9 + need
    for t in range(t0, t0 + need):
        assert step(sm, t, verdict(enu=(2.3, 0.1)), vision(2.0, 0.0))["state"] != GNSS_OK
    out = step(sm, t0 + need, verdict(enu=(2.3, 0.1)), vision(2.0, 0.0))
    assert out["state"] == GNSS_OK and out["release_spoofing"] and "spoof latch released" in out["reasons"][0]
    assert not sm.gnss_blocked


def test_spoof_without_camera_released_by_detector_and_jam_escalation(cfg):
    sm = StateMachine(cfg, camera=False)
    step(sm, 0, verdict())
    assert step(sm, 1, verdict(spoof="ALARM"))["state"] == SPOOFED and not sm.gnss_blocked
    assert step(sm, 2, verdict(spoof="SUSPECT", spoof_score=0.1))["state"] == SPOOFED
    out = step(sm, 3, verdict())
    assert out["state"] == GNSS_OK and "detector latch" in out["reasons"][0]
    sm = StateMachine(cfg, camera=True)
    step(sm, 0, verdict())
    assert step(sm, 1, verdict(jam="ALARM", fix=False, enu=None))["state"] == JAMMED
    out = step(sm, 2, verdict(jam="ALARM", spoof="ALARM"))
    assert out["state"] == SPOOFED and out["cause"] == "spoofing" and out["escalated"] and sm.gnss_blocked


def test_low_texture_gives_no_false_motion(cfg):
    """GNSS jammed and the ground turns smooth while the rig keeps walking: the camera track stops
    (vision_lost) instead of inventing motion, so neither the position nor the state moves on."""
    nav, sm = VisionNav(cfg), StateMachine(cfg, camera=True)
    traj, smooth = synth_walk.Trajectory(straight="20:1.2:90", still_s=3), synth_walk.Renderer(flat=True)
    tex, last_g, rows = synth_walk.Renderer(), -1, []
    for t, p, img in synth_walk.frames(traj, tex, 16, 15):
        nav.on_frame(t, smooth.frame(p) if t >= 10 else img)
        if int(t) == last_g:
            continue
        last_g = int(t)
        jammed = t >= 9
        v = verdict(jam="ALARM" if jammed else "CLEAR", fix=not jammed, enu=None if jammed else (p.east, p.north))
        v["cog_deg"], v["sog_mps"] = 90.0, 1.2 if t >= 3 else 0.0
        if sm.gnss_blocked:
            v["gnss_trusted"] = False
        nav.on_gnss(t, v)
        vis = nav.state(at=t)
        rows.append((t, p.east, step(sm, t, v, vis, nav.breadcrumbs()), vis))
    after = [(t, e, o, vis) for t, e, o, vis in rows if t >= 11]
    moved = after[-1][2]["fused"]["e"] - after[0][2]["fused"]["e"]
    print(f"smooth ground from t=10 s: rig walked {after[-1][1] - after[0][1]:.1f} m, fused position moved {moved:.2f} m")
    assert after[-1][1] - after[0][1] > 4 and abs(moved) < 0.1
    assert all(vis["flag"] == "vision_lost" and o["fused"]["source"] == "vision_stale" for _, _, o, vis in after)
    assert {o["state"] for *_, o, _ in rows} <= {GNSS_OK, JAMMED, VISION_NAV}  # never guided home on a frozen track
    _, ve, vn, _ = nav.vo.velocity()
    assert math.hypot(ve, vn) < 0.05


# ---------------------------------------------------------------- rendered walks through the pipeline
OUTBACK, STILL_S, FPS = "outback:12:90:1.2", 30, 15
ONSET = STILL_S + 9                                       # near the far end (12 m at 1.2 m/s = 10 s)
ARRIVE = STILL_S + 20                                     # back at the start


@pytest.fixture(scope="module")
def outback(tmp_path_factory):
    out = tmp_path_factory.mktemp("outback")
    synth_walk.main([str(out), "--walk", OUTBACK, "--still-s", str(STILL_S), "--duration-s", "100", "--fps", str(FPS)])
    with open(out / "truth.csv", encoding="ascii") as f:
        truth = [(float(r["t"]), float(r["east"]), float(r["north"])) for r in csv.DictReader(f)]
    return out, truth


def run_walk(tmp_path, cfg, outback, duration_s, *profile):
    video, truth = outback
    a = fake_l86.build_parser().parse_args(["--write", str(tmp_path / "walk.nmea"), "--start", START,
                                            "--duration-s", str(duration_s), "--walk", OUTBACK,
                                            "--still-s", str(STILL_S), *profile])
    t0 = datetime.fromisoformat(START.replace("Z", "+00:00"))
    fake_l86.write_file(a, fake_l86.load_cycles(fake_l86.FIXTURE), t0)
    s = replay(tmp_path / "walk.nmea", fast_vision_cfg(cfg), tmp_path / "s", video=video / "video.avi")
    eps = read_jsonl(tmp_path / "s" / "epochs.jsonl")
    for v in eps:                                         # the rig's true position at the epoch's host time
        k = min(len(truth) - 1, round((v["mono"] - t0.timestamp()) * FPS))
        v["_truth"] = truth[k][1:]
    return s, eps, read_jsonl(tmp_path / "s" / "events.jsonl")


def first(eps, state):
    return next(v for v in eps if v["state"] == state)


def check_return_home(eps, ev, alarm):
    nav = [v for v in eps if v["state"] in (VISION_NAV, RETURN_HOME, HOME_REACHED)]
    err = [math.hypot(v["fused"]["e"] - v["_truth"][0], v["fused"]["n"] - v["_truth"][1]) for v in nav]
    assert all(v["fused"]["source"] == "vision" for v in nav)
    assert ONSET <= first(eps, alarm)["seq"] <= ONSET + 1 and first(eps, VISION_NAV)["seq"] <= ONSET + 2
    route = next(e["route"] for e in ev if e["kind"] == "guidance")
    assert route[-1]["id"] == "home" and [w["id"] for w in route[:-1]] == sorted((w["id"] for w in route[:-1]), reverse=True)
    assert [w["e"] for w in route] == sorted((w["e"] for w in route), reverse=True) and len(route) >= 4
    guided = [v for v in eps if v["state"] == RETURN_HOME and v["fused"]["e"] > 2]
    assert guided and all(abs(v["guidance"]["home_bearing_deg"] - 270) < 10 for v in guided)
    assert sum(e["kind"] == "waypoint" for e in ev) >= 3
    home = first(eps, HOME_REACHED)
    assert ARRIVE - 2 <= home["seq"] <= ARRIVE + 4 and math.hypot(*home["_truth"]) < 1.0
    assert any(e["kind"] == "home_confirmed" for e in ev)
    print(f"{alarm}: vision position error over the return: median {sorted(err)[len(err) // 2]:.2f} m, "
          f"max {max(err):.2f} m; HOME_REACHED {home['seq'] - ARRIVE:+d} s after the true arrival")
    assert max(err) < 0.6


def test_jam_walk_returns_home_then_gnss_ok(tmp_path, cfg, outback):
    s, eps, ev = run_walk(tmp_path, cfg, outback, 85, "--jam", "sudden", "--jam-at", str(ONSET),
                          "--recover-after", "30")
    assert s["states"] == [JAMMED if x is None else x for x in CHAIN]
    ok = first([v for v in eps if v["seq"] > ONSET], GNSS_OK)
    assert ok["seq"] > ONSET + 30 and ok["guidance"] is None and ok["fused"]["source"] == "gnss"
    check_return_home(eps, ev, JAMMED)


def test_spoof_walk_returns_home_and_latch_waits_for_agreement(tmp_path, cfg, outback):
    end = ONSET + 42                                      # past the detector's own 30 s latch + holds
    s, eps, ev = run_walk(tmp_path, cfg, outback, 100, "--spoof", "jump", "--spoof-cn0", "natural",
                          "--spoof-at", str(ONSET), "--recover-after", "42")
    assert s["states"] == [SPOOFED if x is None else x for x in CHAIN]
    check_return_home(eps, ev, SPOOFED)
    blocked = [v for v in eps if v["spoofing"]["level"] == "CLEAR" and ONSET < v["seq"] < end]
    assert blocked and all(not v["gnss_trusted"] and v.get("trust_blocked") and v["state"] == HOME_REACHED
                           for v in blocked)                   # the detector cleared; the state machine did not
    assert all(v["fused"]["source"] != "gnss" for v in eps if ONSET <= v["seq"] < end)
    released = first([v for v in eps if v["seq"] > ONSET], GNSS_OK)
    need = cfg["state"]["spoof_release"]["agree_s"]
    print(f"spoof ended at {end} s, latch released at {released['seq']} s")
    assert end + need <= released["seq"] <= end + need + 3
    tr = next(e for e in ev if e["kind"] == "state" and e["seq"] == released["seq"])
    assert "spoof latch released" in tr["reasons"][0]
    after = [v for v in eps if v["seq"] > released["seq"]]
    assert after and all(v["gnss_trusted"] and v["spoofing"]["level"] == "CLEAR" for v in after)
    assert all(math.hypot(v["enu_m"][0] - v["_truth"][0], v["enu_m"][1] - v["_truth"][1]) < 1.5 for v in after)
    d = __import__("json").loads((tmp_path / "s" / "dashboard.json").read_text(encoding="utf-8"))
    assert [t["state"] for t in d["transitions"]] == s["states"] and len(d["routes"]) == 1
    assert any(r["guidance"] for r in d["timeline"]) and any(r["latch"] for r in d["timeline"])


def test_clean_walk_no_false_alarm_from_the_displacement_check(tmp_path, cfg, outback):
    s, eps, ev = run_walk(tmp_path, cfg, outback, 95)
    drift = [v["features"]["vision_pos_drift_m"] for v in eps if v["features"]["vision_pos_drift_m"] is not None]
    print(f"clean out-and-back walk: GNSS-vs-camera displacement max {max(drift):.2f} m over {len(drift)} epochs")
    assert len(drift) > 30 and max(drift) < 1.5
    assert all(v["spoofing"]["level"] == "CLEAR" and v["jamming"]["level"] == "CLEAR" for v in eps)
    assert s["states"] == [WARMING_UP, GNSS_OK]
