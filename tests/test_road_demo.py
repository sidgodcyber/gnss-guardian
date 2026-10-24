"""The road demo end to end, synthetic: stand at the start (warm-up, home, home keyframe), walk 30 m
out with GNSS trusted, a demo drift spoof is injected (DemoInjector on the NMEA, as live
--demo-inject does), the camera cross-check catches it, VISION_NAV -> RETURN_HOME, the walker
follows the guidance back along the keyframes, HOME_REACHED on the home keyframe match; the
injection stops and the spoof latch releases only once GNSS and the camera agree again.

Rendered ground (a 38 x 6 m strip) and simulated GNSS: this checks the logic, not field performance.
"""
import csv
import math
from datetime import datetime

import pytest
from conftest import START, fake_l86, fast_vision_cfg, synth_walk

from gnss_guardian.inject import DemoInjector
from gnss_guardian.io.nmea import split_prefix
from gnss_guardian.run import replay
from gnss_guardian.session import read_jsonl
from gnss_guardian.state import GNSS_OK, HOME_REACHED, RETURN_HOME, SPOOFED, VISION_NAV, WARMING_UP

WALK, STILL_S, FPS, DURATION = "outback:30:90:1.2", 30, 10, 118
INJECT_AT, STOP_AT = 50, 88                     # 24 m out; stopped 8 s after the walker is back home
FAR_END, BACK = STILL_S + 25, STILL_S + 50


@pytest.fixture(scope="module")
def road(tmp_path_factory):
    out = tmp_path_factory.mktemp("road")
    synth_walk.main([str(out), "--walk", WALK, "--still-s", str(STILL_S), "--duration-s", str(DURATION),
                     "--fps", str(FPS), "--world=-4:34:-3:3"])
    with open(out / "truth.csv", encoding="ascii") as f:
        truth = [(float(r["t"]), float(r["east"]), float(r["north"])) for r in csv.DictReader(f)]
    return out, truth


def inject(src, dst, t0):
    """As live --demo-inject: drift 1.0 m/s due north from INJECT_AT to STOP_AT, on the detector's copy."""
    host_s = [0.0]
    inj = DemoInjector(clock=lambda: host_s[0])
    lines = []
    for line in src.read_text(encoding="ascii").splitlines():
        _, host, sentence = split_prefix(line)
        host_s[0] = (host.timestamp() - t0) if host else host_s[0]
        if INJECT_AT <= host_s[0] < STOP_AT and inj.active is None:
            inj.start("drift", bearing_deg=0, mps=1.0)
        elif host_s[0] >= STOP_AT and inj.active is not None:
            inj.stop()
        lines.append(line[:len(line) - len(sentence)] + inj.apply(sentence))
    dst.write_text("\n".join(lines) + "\n", encoding="ascii")
    return dst


def test_road_walk_spoof_detected_guided_home(tmp_path, cfg, road):
    video, truth = road
    t0 = datetime.fromisoformat(START.replace("Z", "+00:00"))
    a = fake_l86.build_parser().parse_args(["--write", str(tmp_path / "real.nmea"), "--start", START, "--duration-s",
                                            str(DURATION), "--walk", WALK, "--still-s", str(STILL_S)])
    fake_l86.write_file(a, fake_l86.load_cycles(fake_l86.FIXTURE), t0)
    src = inject(tmp_path / "real.nmea", tmp_path / "detector_input.nmea", t0.timestamp())
    s = replay(src, fast_vision_cfg(cfg), tmp_path / "s", video=video / "video.avi")
    eps = read_jsonl(tmp_path / "s" / "epochs.jsonl")
    ev = read_jsonl(tmp_path / "s" / "events.jsonl")
    for v in eps:
        k = min(len(truth) - 1, round((v["mono"] - t0.timestamp()) * FPS))
        v["_t"], v["_truth"] = truth[k][0], truth[k][1:]

    for v in eps:
        if v["_t"] >= STOP_AT - 1 and v["seq"] % 4 == 0:
            print(f"{v['_t']:.0f} s {v['state']} spoof {v['spoofing']['level']} {v['spoofing']['score']:.2f} "
                  f"latch {v['latch'] and v['latch']['detail']}")
    chain = [x for x in s["states"] if x != "SUSPECT"]
    print("states:", " -> ".join(s["states"]))
    assert chain == [WARMING_UP, GNSS_OK, SPOOFED, VISION_NAV, RETURN_HOME, HOME_REACHED, GNSS_OK]
    home_kf = next(e for e in ev if e["kind"] == "keyframe" and e["home"])
    assert home_kf["features"] >= cfg["keyframes"]["home_min_features"] and math.hypot(home_kf["e"], home_kf["n"]) < 0.5
    out = [v for v in eps if STILL_S + 2 < v["_t"] < INJECT_AT]
    assert out and all(v["gnss_trusted"] for v in out) and max(v["_truth"][0] for v in out) > 20

    alarm = next(v for v in eps if v["state"] == SPOOFED)
    print(f"injected at {INJECT_AT} s, SPOOFED at {alarm['_t']:.0f} s: {alarm['spoofing']['reasons']}")
    assert INJECT_AT < alarm["_t"] <= INJECT_AT + 5
    assert any("vision" in r for r in alarm["spoofing"]["reasons"])                 # the camera cross-check
    assert next(v for v in eps if v["state"] == VISION_NAV)["_t"] <= INJECT_AT + 6

    nav = [v for v in eps if v["state"] in (VISION_NAV, RETURN_HOME, HOME_REACHED) and v["_t"] < STOP_AT]
    err = [math.hypot(v["fused"]["e"] - v["_truth"][0], v["fused"]["n"] - v["_truth"][1]) for v in nav]
    print(f"vision position error over the return: median {sorted(err)[len(err) // 2]:.2f} m, max {max(err):.2f} m")
    assert all(v["fused"]["source"] == "vision" for v in nav) and max(err) < 1.5
    back = [v for v in eps if v["state"] == RETURN_HOME and FAR_END + 2 < v["_t"] < BACK - 3]
    assert back and all(abs(v["guidance"]["home_bearing_deg"] - 270) < 10 for v in back)        # walk west, home
    assert all(v["guidance"]["heading_aligned"] for v in back)
    assert sum(e["kind"] == "waypoint" for e in ev) >= 8

    home = next(v for v in eps if v["state"] == HOME_REACHED)
    print(f"HOME_REACHED at {home['_t']:.0f} s (back at {BACK} s), true position {home['_truth']}")
    assert BACK - 2 <= home["_t"] <= BACK + 4 and math.hypot(*home["_truth"]) < 1.0
    hc = next(e for e in ev if e["kind"] == "home_confirmed")
    assert hc["inliers"] >= cfg["keyframes"]["home_match"]["min_inliers"]

    blocked = [v for v in eps if STOP_AT <= v["_t"] and v["state"] != GNSS_OK]
    released = next(v for v in eps if v["_t"] > STOP_AT and v["state"] == GNSS_OK)
    print(f"injection stopped at {STOP_AT} s, latch released at {released['_t']:.0f} s")
    need = cfg["state"]["spoof_release"]["agree_s"]
    assert blocked and all(not v["gnss_trusted"] for v in blocked)
    flush = cfg["detect"]["spoofing"]["vision_long_window_s"]      # the drift-era velocity samples age out first
    assert STOP_AT + need <= released["_t"] <= STOP_AT + flush + need + 4
    tr = next(e for e in ev if e["kind"] == "state" and e["state"] == GNSS_OK and e["seq"] == released["seq"])
    assert "spoof latch released" in tr["reasons"][0]
