"""Demo sky injection (gnss_guardian/inject.py, kind "sky"): the GPS satellite list rewritten before the
detector, position untouched, for showing the sky check on the road without a spoofer.

  rewrite      a whole $GPGSV group: azimuths rotated, ghost PRNs (below -10 deg in the almanac at the
               last real fix) added, counts and checksums redone; position, time, GLONASS unchanged
  controls     rot / ghosts limits, ghosts need the almanac and a fix, stop mid-group passes it on
  detector     simulator walk with the almanac sky: CLEAR before, ALARM within 3 s after (15 deg + 2
               ghosts, either alone), nothing with the sky check off (the position never moves)
  live         acceptance: live run against the simulator, camera OFF, Sky from the HTTP endpoint ->
               SPOOFED from the sky check, position unchanged, GNSS Trust < 20; Stop -> GNSS_OK, CLEAR
"""
import copy
import json
import threading
import urllib.error

import pytest
from conftest import FIX, nmea, run_engine, sim_file
from test_live_display import event, get, wait_for
from test_live_sim import start_sim

from gnss_guardian.dashboard_export import _injection_text
from gnss_guardian.detect.sky import parse_yuma, predict
from gnss_guardian.inject import DemoInjector
from gnss_guardian.io.nmea import checksum_ok
from gnss_guardian.io.serial_live import run_live
from gnss_guardian.session import read_jsonl

ALM = FIX / "yuma_2026_295.alm"                       # week of the simulator's start, 2026-10-22
RMC = nmea("GPRMC,140000.000,A,2915.3360,N,07932.2200,E,0.02,0.00,221026,,,A")
GGA = nmea("GPGGA,140000.000,2915.3360,N,07932.2200,E,1,11,0.9,500.0,M,-40.0,M,,")
GSV = [nmea("GPGSV,3,1,10,01,17,043,34,03,06,087,31,06,37,183,40,07,11,153,35"),
       nmea("GPGSV,3,2,10,11,09,200,33,14,56,060,45,17,64,006,47,19,62,280,47"),
       nmea("GPGSV,3,3,10,20,15,251,32,22,60,356,46")]
GL = nmea("GLGSV,1,1,02,65,50,100,38,72,35,230,33")


@pytest.fixture(scope="module")
def alm():
    return parse_yuma(ALM)


def sky_cfg(cfg, enabled=True):
    c = copy.deepcopy(cfg)
    c["detect"]["sky"].update(almanac_file=str(ALM), enabled=enabled)
    return c


def sats(lines):
    out = {}
    for ln in lines:
        f = ln[1:ln.rindex("*")].split(",")
        for i in range(4, len(f) - 3, 4):
            out[f[i]] = (int(f[i + 1]), int(f[i + 2]), int(f[i + 3]))
    return out


def test_rewrites_the_gps_group_only(alm):
    inj = DemoInjector(almanac=alm)
    for ln in (RMC, GGA, *GSV, GL):
        assert inj.feed(ln) == [ln]                                     # nothing active: unchanged
    a = inj.start("sky", rot_deg=15, ghosts=2)
    assert a["type"] == "sky" and a["offset_m"] == 0 and a["bearing_deg"] is None
    assert a["label"] == f"sky: GPS azimuths +15 deg, 2 ghost satellites ({' '.join(a['ghost_prns'])})"
    pred = predict(alm, 1792677600.0, 29.2556, 79.537, 500.0)          # 2026-10-22T14:00:00Z, the RMC's fix
    assert all(pred[int(g[1:])][0] < -10 for g in a["ghost_prns"])
    for ln in (RMC, GGA, GL):
        assert inj.feed(ln) == [ln]                                     # position, time, GLONASS stay real
    assert inj.feed(GSV[0]) == [] and inj.feed(GSV[1]) == []            # buffered until the group is complete
    out = inj.feed(GSV[2])
    assert len(out) == 3 and all(checksum_ok(x) for x in out)
    assert [x.split(",")[1:4] for x in out] == [["3", str(k), "12"] for k in (1, 2, 3)]
    before, after = sats(GSV), sats(out)
    for prn, (el, az, snr) in before.items():
        assert after[prn] == (el, (az + 15) % 360, snr)
    ghosts = {p: after[p[1:]] for p in a["ghost_prns"]}
    assert [g[0] for g in ghosts.values()] == [25, 40] and all(30 <= g[2] <= 50 for g in ghosts.values())
    inj.stop()
    assert inj.feed(GSV[0]) == [GSV[0]]


def test_controls(alm):
    inj = DemoInjector(almanac=alm)
    with pytest.raises(ValueError, match="no GNSS fix"):
        inj.start("sky", rot_deg=15, ghosts=2)
    assert inj.start("sky", rot_deg=15, ghosts=0)["ghost_prns"] == []   # rotation alone needs no fix
    inj.feed(RMC)
    for kw, msg in ((dict(rot_deg=200), "rot"), (dict(rot_deg=0, ghosts=0), "rotation or"),
                    (dict(ghosts=5), "ghosts in")):
        with pytest.raises(ValueError, match=msg):
            inj.start("sky", **kw)
    with pytest.raises(ValueError, match="almanac"):
        DemoInjector().start("sky", rot_deg=15, ghosts=1)
    a = inj.start("sky")                                                # defaults: 15 deg, 2 ghosts
    assert (a["rot_deg"], a["ghosts"], len(a["ghost_prns"])) == (15, 2, 2)
    assert inj.feed(GSV[0]) == []
    inj.stop()                                                          # stopped mid-group: passed on as received
    assert inj.feed(GSV[1]) == [GSV[0], GSV[1]]
    assert _injection_text({"action": "sky", "label": a["label"]}).startswith(f"DEMO INJECTION {a['label']} (")


def inject_halfway(lines, alm, **kw):
    """As live --demo-inject: the detector's copy, sky injection from the middle of the file."""
    inj, out, k = DemoInjector(almanac=alm), [], len(lines) // 2
    for i, ln in enumerate(lines):
        pre, line = ln.split(",", 1)
        if i == k:
            inj.start("sky", **kw)
        out += [f"{pre},{x}" for x in inj.feed(line)]
    return out


@pytest.mark.parametrize("kw", [dict(rot_deg=15, ghosts=2), dict(rot_deg=15, ghosts=0), dict(rot_deg=0, ghosts=2)],
                         ids=["rot15+2ghosts", "rot15", "2ghosts"])
def test_detector_catches_it_without_camera(tmp_path, cfg, alm, kw):
    lines = sim_file(tmp_path / "w.nmea", "--sky", f"almanac:{ALM}", duration_s=200).read_text().splitlines()
    det = inject_halfway(lines, alm, **kw)
    pos = lambda ls: [x.split(",", 1)[1] for x in ls if "RMC" in x or "GGA" in x]   # noqa: E731
    assert pos(det) == pos(lines)                                                     # the position never moves
    v = run_engine(det, sky_cfg(cfg))
    k = len(v) // 2                                                                   # 1 Hz: epoch k = injection
    assert all(x["spoofing"]["level"] == "CLEAR" for x in v[:k - 1] if not x["warming_up"])
    alarm = next(x for x in v if x["spoofing"]["level"] == "ALARM")
    print(kw, f"ALARM {alarm['seq'] - v[k]['seq']} s after the injection:", alarm["spoofing"]["reasons"])
    assert 0 <= alarm["seq"] - v[k]["seq"] <= 3
    off = run_engine(det, sky_cfg(cfg, enabled=False))                                # the sky check is what sees it
    assert all(x["spoofing"]["level"] == "CLEAR" for x in off if not x["warming_up"])


SPEED = 8


def test_live_sky_injection_acceptance(tmp_path, cfg):
    """Live against the simulator (almanac sky), camera OFF: Sky 15 deg + 2 ghosts from the guide page's
    endpoint -> SPOOFED from the sky check with the position unchanged and GNSS Trust < 20; Stop ->
    back to GNSS_OK with spoofing CLEAR once the detector's latch (min_alarm_s) has run out."""
    c = sky_cfg(cfg)
    c["live"]["reconnect_s"] = 0.1
    p, port = start_sim("--duration-s", "600", "--speed", str(SPEED), "--sky", f"almanac:{ALM}")
    out = tmp_path / "s"
    th = threading.Thread(target=run_live, args=(port, 9600, out, c), daemon=True,
                          kwargs=dict(clock_scale=SPEED, duration_s=45, overlay_port=0, demo_inject=True,
                                      echo=lambda s: None))
    th.start()
    try:
        srv = wait_for(lambda: event(out, "display_server"))
        base = f"http://127.0.0.1:{srv['port']}"

        def live():
            try:
                return json.loads(get(f"{base}/live.json")[2])
            except urllib.error.HTTPError:
                return {"state": None, "spoofing": {"level": "CLEAR"}}
        wait_for(lambda: live()["state"] == "GNSS_OK", timeout=30)
        assert b"inject('sky')" in get(f"{base}/guide")[2]
        st, _, body = get(f"{base}/inject/sky?rot=15&ghosts=2", data=b"", method="POST")
        a = json.loads(body)["injection"]["active"]
        assert st == 200 and a["type"] == "sky" and len(a["ghost_prns"]) == 2
        v = wait_for(lambda: (x := live())["state"] == "SPOOFED" and x, timeout=10)
        print("sky injection:", v["spoofing"]["reasons"], "trust", v["trust"]["gnss"]["score"])
        assert any("off the almanac sky" in r for r in v["spoofing"]["reasons"])
        assert any("below the horizon" in r for r in v["spoofing"]["reasons"])
        assert v["trust"]["gnss"]["score"] < 20 and v["injection"]["active"]["label"].startswith("sky:")
        st, _, body = get(f"{base}/inject/stop", data=b"", method="POST")
        assert st == 200 and json.loads(body)["injection"]["active"] is None
        v = wait_for(lambda: (x := live())["state"] == "GNSS_OK" and x["gnss_trusted"] and x, timeout=25)
        assert v["spoofing"]["level"] == "CLEAR" and v["trust"]["gnss"]["score"] >= 80
    finally:
        th.join(timeout=60)
        p.kill()
        p.wait(timeout=10)
    assert not th.is_alive()
    ev = read_jsonl(out / "events.jsonl")
    assert [e["action"] for e in ev if e["kind"] == "demo_inject"] == ["sky", "stop"]
    raw = "".join(f.read_text() for f in sorted((out / "raw").glob("*.nmea"))).splitlines()
    det = "".join(f.read_text() for f in sorted((out / "detector_input").glob("*.nmea"))).splitlines()
    pos = lambda ls: [x.split(",", 1)[1] for x in ls if "RMC" in x or "GGA" in x]   # noqa: E731
    assert pos(det) == pos(raw)                                                       # position unchanged
    assert {x for x in det if "GPGSV" in x} - {x for x in raw if "GPGSV" in x}        # the sky was rewritten
    eps = read_jsonl(out / "epochs.jsonl")
    assert eps[-1]["state"] == "GNSS_OK" and eps[-1]["spoofing"]["level"] == "CLEAR"
