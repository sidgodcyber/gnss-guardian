"""Live mode's display side against the serial simulator: the overlay stream, the guidance page,
live.json, demo injection through the HTTP endpoints, and a busy port that must not stop live mode.
The simulator runs SPEED x real time with the matching --clock-scale; the camera is a still video."""
import copy
import json
import socket
import threading
import time
import urllib.request

import pytest
from conftest import fast_vision_cfg, synth_walk
from test_live_sim import start_sim

from click.testing import CliRunner

from gnss_guardian.cli import cli
from gnss_guardian.io.serial_live import run_live
from gnss_guardian.session import read_jsonl
from gnss_guardian.status import status_line

SPEED = 4


@pytest.fixture(scope="module")
def still(tmp_path_factory):
    out = tmp_path_factory.mktemp("still_live")
    synth_walk.main([str(out), "--still-s", "40", "--duration-s", "40", "--fps", "10"])
    return out / "video.avi"


@pytest.fixture
def live_cfg(cfg):
    c = fast_vision_cfg(cfg)
    c["live"]["reconnect_s"] = 0.1
    return c


def wait_for(cond, timeout=30.0, every=0.1):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        r = cond()
        if r:
            return r
        time.sleep(every)
    raise AssertionError("timed out")


def event(folder, kind):
    p = folder / "events.jsonl"
    if not p.exists():
        return None
    return next((e for e in read_jsonl(p) if e["kind"] == kind), None)


def get(url, data=None, method=None):
    req = urllib.request.Request(url, data=data, method=method)
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, r.headers, r.read()


def test_live_display_guide_and_demo_injection(tmp_path, live_cfg, still):
    p, port = start_sim("--duration-s", "200", "--speed", str(SPEED))      # argparse: the last --speed wins
    out = tmp_path / "s"
    th = threading.Thread(target=run_live, args=(port, 9600, out, live_cfg), daemon=True,
                          kwargs=dict(clock_scale=SPEED, duration_s=25, camera=f"video:{still}", overlay_port=0,
                                      overlay_record=True, demo_inject=True, echo=lambda s: None))
    th.start()
    try:
        srv = wait_for(lambda: event(out, "display_server"))
        base = f"http://127.0.0.1:{srv['port']}"

        def live():
            try:
                return json.loads(get(f"{base}/live.json")[2])
            except urllib.error.HTTPError:                                         # 503 before the first epoch
                return {"home": None, "state": None, "spoofing": {"level": "CLEAR"}}
        v = wait_for(lambda: (x := live()) and x["home"] and x["home"]["set"] and x["state"] == "GNSS_OK" and x)
        assert '"lat"' not in json.dumps(v) and '"lon"' not in json.dumps(v)
        assert v["injection"] == {"enabled": True, "active": None} and v["row"]["state"] == "GNSS_OK"
        st, hdr, body = get(f"{base}/guide")
        assert st == 200 and b"DEMO INJECTION ACTIVE" in body and hdr["Content-Type"].startswith("text/html")
        with urllib.request.urlopen(f"{base}/", timeout=5) as r:                       # MJPEG stream
            assert r.headers["Content-Type"].startswith("multipart/x-mixed-replace")
            head = r.read(200)
            assert b"Content-Type: image/jpeg" in head and b"\xff\xd8" in head
        st, _, body = get(f"{base}/inject/drift?mps=1.0&bearing=90", data=b"", method="POST")
        assert st == 200 and json.loads(body)["injection"]["active"]["type"] == "drift"
        v = wait_for(lambda: (x := live())["spoofing"]["level"] != "CLEAR" and x, timeout=20)
        assert v["injection"]["active"]["offset_m"] > 2
        assert "DEMO INJECTION ACTIVE (drift" in status_line(out)[0]
        st, _, body = get(f"{base}/inject/stop", data=b"", method="POST")
        assert st == 200 and json.loads(body)["injection"]["active"] is None
        with pytest.raises(urllib.error.HTTPError) as e:
            get(f"{base}/inject/drift?mps=-1&bearing=90", data=b"", method="POST")
        assert e.value.code == 400
    finally:
        th.join(timeout=40)
        p.kill()
        p.wait(timeout=10)
    assert not th.is_alive()
    ev = read_jsonl(out / "events.jsonl")
    inj = [e for e in ev if e["kind"] == "demo_inject"]
    assert [e["action"] for e in inj] == ["drift", "stop"] and inj[0]["mps"] == 1.0 and inj[0]["bearing_deg"] == 90
    assert any(e["kind"] == "overlay_stopped" and e["frames"] > 5 and not e["error"] for e in ev)
    raw = "".join(f.read_text() for f in (out / "raw").glob("*.nmea"))
    det = "".join(f.read_text() for f in (out / "detector_input").glob("*.nmea"))
    assert raw.count("\n") == det.count("\n") and raw != det                     # same lines, positions moved
    rmc = [ln.split(",")[4:6] for ln in raw.splitlines() if "RMC" in ln and ",A," in ln]
    assert len({tuple(x) for x in rmc}) == 1                                       # raw: the real (static) fix
    assert json.loads((out / "live.json").read_text())["format"] == "gnss-guardian-live/1"

    # the field run opens in the dashboard afterwards: replay on the live folder exports what live recorded
    r = CliRunner().invoke(cli, ["replay", str(out)])
    assert r.exit_code == 0, r.output
    d = json.loads((out / "dashboard.json").read_text(encoding="utf-8"))
    assert d["meta"]["mode"] == "live" and d["meta"]["video"] == "dashboard_video.webm"
    assert any(row["injection"] and row["injection"]["type"] == "drift" for row in d["timeline"])
    assert [e["detail"].split(" (")[0] for e in d["events"] if e["kind"] == "demo_inject"] == [
        "DEMO INJECTION drift 1.0 m/s at 90.0 deg", "demo injection stopped"]
    assert any(row["frame"] is not None for row in d["timeline"])
    r = CliRunner().invoke(cli, ["replay", str(out), "--rerun", "--out", str(tmp_path / "rerun")])
    assert r.exit_code == 0 and "detector_input.nmea" in r.output, r.output
    assert (tmp_path / "rerun" / "dashboard.json").exists()


def test_busy_overlay_port_does_not_stop_live(tmp_path, live_cfg):
    busy = socket.socket()
    busy.bind(("0.0.0.0", 0))
    busy.listen(1)
    port_no = busy.getsockname()[1]
    p, port = start_sim("--duration-s", "60")
    try:
        s = run_live(port, 9600, tmp_path, live_cfg, clock_scale=20, duration_s=60 / 20 + 1.0, overlay_port=port_no,
                     echo=lambda s: None)
    finally:
        p.kill()
        p.wait(timeout=10)
        busy.close()
    ev = read_jsonl(tmp_path / "events.jsonl")
    assert any(e["kind"] == "overlay_unavailable" and e["port"] == port_no for e in ev)
    assert s["epochs"] >= 50 and (tmp_path / "live.json").exists()


def test_injector_moves_only_the_position():
    from conftest import nmea

    from gnss_guardian import geo
    from gnss_guardian.inject import DemoInjector
    from gnss_guardian.io.nmea import checksum_ok
    t = [0.0]
    inj = DemoInjector(clock=lambda: t[0])
    rmc = nmea("GPRMC,140000.000,A,2915.3360,N,07932.2200,E,0.02,0.00,221026,,,A")
    gga = nmea("GPGGA,140000.000,2915.3360,N,07932.2200,E,1,11,0.9,500.0,M,-40.0,M,,")
    gsv = nmea("GPGSV,3,1,11,02,62,040,45,05,30,120,38,07,15,200,30,09,70,300,44")
    assert inj.apply(rmc) == rmc
    inj.start("jump", bearing_deg=90, m=30)

    def ll(s, i):
        f = s.split(",")
        return (float(f[i][:2]) + float(f[i][2:]) / 60, float(f[i + 2][:3]) + float(f[i + 2][3:]) / 60)
    for line, i in ((rmc, 3), (gga, 2)):
        out = inj.apply(line)
        assert checksum_ok(out) and out != line
        e, n = geo.enu(*ll(out.split("*")[0], i), *ll(line.split("*")[0], i))
        assert abs(e - 30) < 0.3 and abs(n) < 0.3
    assert inj.apply(gsv) == gsv                                   # signal strengths stay real
    inj.start("drift", bearing_deg=0, mps=0.5)
    t[0] = 10.0
    out = inj.apply(rmc).split(",")
    e, n = geo.enu(*ll(",".join(out), 3), *ll(rmc, 3))
    assert abs(n - 5.0) < 0.3 and abs(float(out[7]) * 0.514444 - 0.52) < 0.03   # reported speed follows
    assert inj.status()["active"]["offset_m"] == 5.0
    assert inj.stop()["type"] == "drift" and inj.apply(rmc) == rmc
