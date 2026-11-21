"""GNSS Trust and Nav confidence (gnss_guardian/trust.py): display numbers with their reasons.

  formula      penalties and caps from config; every point below 100 is explained (a penalty or the cap)
  invariant    GNSS Trust >= 80 only when GNSS is trusted, on every simulator profile through the pipeline;
               <= the state cap in every alarm state
  nav          only while the camera carries the position; quality, drift since the last absolute fix (reset
               by a keyframe match), unaligned heading, and the cap when the track is lost
  camera       a rendered walk with a jam: Nav confidence present exactly while the fused position is vision
  outputs      trust reaches epochs.jsonl, dashboard rows and live.json
"""
import copy
import json

import pytest
from conftest import fast_vision_cfg, sim_file, synth_walk

from gnss_guardian.livehub import LiveHub
from gnss_guardian.run import replay, resolve_recording
from gnss_guardian.session import read_jsonl
from gnss_guardian.trust import NavConfidence, gnss_trust


def verdict(state="GNSS_OK", trusted=True, js=0.0, ss=0.0, fix=True, hdop_ratio=1.0, **kw):
    return {"state": state, "cause": kw.get("cause"), "gnss_trusted": trusted, "fix_valid": fix,
            "jamming": {"score": js, "level": "CLEAR" if js < 0.35 else "ALARM", "reasons": ["C/N0 down"] if js else []},
            "spoofing": {"score": ss, "level": "CLEAR" if ss < 0.35 else "ALARM", "reasons": ["jump"] if ss else []},
            "features": {"hdop_ratio": hdop_ratio}, **{k: v for k, v in kw.items() if k != "cause"}}


def explained(t):
    lost = 100 - t["score"]
    pts = sum(p["points"] for p in t["penalties"])
    return t["cap"] is not None or abs(lost - pts) <= 1.0 or (t["score"] == 0 and pts >= 100)


def test_clean_is_100(cfg):
    t = gnss_trust(verdict(), cfg)
    assert t == {"score": 100, "band": "high", "penalties": [], "cap": None}


def test_penalties(cfg):
    w = cfg["trust"]["gnss"]["weights"]
    t = gnss_trust(verdict(js=0.2, ss=0.1, hdop_ratio=2.25), cfg)
    assert [p["code"] for p in t["penalties"]] == ["JAMMING", "SPOOFING", "HDOP"]
    assert t["score"] == round(100 - 0.2 * w["jamming"] - 0.1 * w["spoofing"] - 0.5 * w["hdop"])
    assert explained(t)
    t = gnss_trust(verdict(ai={"score": 0.5, "reasons": ["cn0_std low"]}), cfg)
    assert t["penalties"][0]["code"] == "AI" and t["score"] == 100 - 0.5 * w["ai"]


def test_caps(cfg):
    c = cfg["trust"]["gnss"]
    t = gnss_trust(verdict("SPOOFED", trusted=False, ss=0.9, cause="spoofing"), cfg)
    assert t["score"] <= c["state_caps"]["SPOOFED"] and t["band"] == "low"
    t = gnss_trust(verdict("GNSS_OK", trusted=False, trust_blocked="spoof latch: waiting"), cfg)
    assert t["score"] == c["latch_cap"] and t["cap"]["code"] == "LATCH"
    t = gnss_trust(verdict("WARMING_UP", trusted=False), cfg)
    assert t["score"] == c["state_caps"]["WARMING_UP"] and t["cap"]["code"] == "STATE_WARMING_UP"
    t = gnss_trust(verdict(fix=False, trusted=False), cfg)
    assert t["score"] == 0 and t["cap"] is None and t["penalties"][-1]["code"] == "NO_FIX"


@pytest.mark.parametrize("args", [[], ["--jam", "sudden", "--recover-after", "40"], ["--jam", "ramp"],
                                  ["--spoof", "jump"], ["--spoof", "drift"], ["--spoof", "time", "--recover-after", "40"]],
                         ids=["clean", "jam-sudden", "jam-ramp", "spoof-jump", "spoof-drift", "spoof-time"])
def test_invariant_every_profile(tmp_path, cfg, args):
    replay(sim_file(tmp_path / "p.nmea", *args, duration_s=220), cfg, tmp_path / "s")
    eps = read_jsonl(tmp_path / "s" / "epochs.jsonl")
    caps = cfg["trust"]["gnss"]["state_caps"]
    for v in eps:
        g = v["trust"]["gnss"]
        assert (g["score"] >= 80) <= v["gnss_trusted"], v["seq"]
        if v["state"] in caps:
            assert g["score"] <= caps[v["state"]], (v["seq"], v["state"], g)
        assert explained(g), g
        assert v["trust"]["nav"] is None                        # no camera
    if not args:
        assert all(v["trust"]["gnss"]["score"] == 100 for v in eps if v["gnss_trusted"])


def vis(q=0.9, aligned=True, match_t=None, flag="ok"):
    return {"quality": q, "aligned": aligned, "flag": flag,
            "last_match": None if match_t is None else {"t": match_t}, "home_confirmed": None}


def test_nav_confidence(cfg):
    c = cfg["trust"]["nav"]
    nav = NavConfidence(cfg)
    assert nav.update({"fused": {"source": "gnss", "e": 0, "n": 0}, "vision": vis()}) is None
    r = nav.update({"fused": {"source": "vision", "e": 0.0, "n": 0.0}, "vision": vis(q=0.8)})
    assert r["score"] == round(100 - c["quality_weight"] * 0.2) and r["penalties"][0]["code"] == "QUALITY"
    r = nav.update({"fused": {"source": "vision", "e": 10.0, "n": 0.0}, "vision": vis(q=1.0)})
    assert r["since_fix_m"] == 10.0 and r["score"] == round(100 - c["drift_pts_per_m"] * 10)
    r = nav.update({"fused": {"source": "vision", "e": 11.0, "n": 0.0}, "vision": vis(q=1.0, match_t=5.0)})
    assert r["since_fix_m"] == 0.0 and r["score"] == 100                 # keyframe match: absolute fix
    r = nav.update({"fused": {"source": "vision", "e": 11.0, "n": 0.0}, "vision": vis(q=1.0, match_t=5.0, aligned=False)})
    assert r["score"] == 100 - c["unaligned"]
    r = nav.update({"fused": {"source": "vision_stale", "e": 11.0, "n": 0.0}, "vision": vis(q=0.1, flag="vision_lost")})
    assert r["score"] <= c["stale_cap"] and r["band"] == "low"
    assert nav.update({"fused": {"source": "gnss", "e": 0, "n": 0}, "vision": vis()}) is None
    r = nav.update({"fused": {"source": "vision", "e": 50.0, "n": 0.0}, "vision": vis(q=1.0)})
    assert r["since_fix_m"] == 0.0                                       # restarts after GNSS carried it


def test_camera_session_with_jam(tmp_path, cfg):
    """Rendered walk (stand 30 s, 20 x 10 m loop), sudden jam at 45 s with the fix lost, camera on."""
    synth_walk.main([str(tmp_path / "w"), "--walk", "loop:20:10:1.2", "--still-s", "30", "--duration-s", "75",
                     "--fps", "10", "--nmea", "--nmea-args", "--jam sudden --jam-at 45"])
    nmea, video, frames = resolve_recording(tmp_path / "w")
    replay(nmea, fast_vision_cfg(cfg), tmp_path / "s", video=video, frames_csv=frames)
    eps = read_jsonl(tmp_path / "s" / "epochs.jsonl")
    on_cam = [v for v in eps if (v.get("fused") or {}).get("source") in ("vision", "vision_stale")]
    assert on_cam, [v["state"] for v in eps][40:]
    for v in eps:
        cam = (v.get("fused") or {}).get("source") in ("vision", "vision_stale")
        assert (v["trust"]["nav"] is not None) == cam
        assert (v["trust"]["gnss"]["score"] >= 80) <= v["gnss_trusted"]
    print("nav confidence on the camera:", [v["trust"]["nav"]["score"] for v in on_cam][:20])
    assert all(0 <= v["trust"]["nav"]["score"] <= 100 for v in on_cam)
    d = json.loads((tmp_path / "s" / "dashboard.json").read_text(encoding="utf-8"))
    assert all("trust" in r for r in d["timeline"]) and any(r["trust"]["nav"] for r in d["timeline"])


def test_live_view_carries_trust(cfg, tmp_path):
    hub = LiveHub(cfg, tmp_path)
    v = copy.deepcopy(verdict())
    v.update(seq=1, mono=1.0, t_gnss="2026-10-22T14:00:00Z", warming_up=False, labels=[], hdop=0.9,
             trust={"gnss": gnss_trust(verdict(), cfg), "nav": None})
    v["jamming"].update(parts={}, changed=False)
    v["spoofing"].update(parts={}, changed=False)
    view = hub.update(v)
    assert view["trust"]["gnss"]["score"] == 100 and view["row"]["trust"]["gnss"]["score"] == 100
    assert json.loads((tmp_path / "live.json").read_text())["trust"]["gnss"]["band"] == "high"
