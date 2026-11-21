"""Live mode end to end against the serial simulator over TCP (pyserial socket://, the Windows
stand-in for /dev/serial0). The simulator runs 20x real time and live mode uses the matching
--clock-scale, so 100 simulated seconds take ~5 s."""
import copy
import subprocess
import sys
from datetime import datetime

import pytest
from conftest import ROOT, START, T0

from gnss_guardian.io.serial_live import pmtk353, run_live
from gnss_guardian.session import read_jsonl

SPEED = 20


def start_sim(*args):
    p = subprocess.Popen([sys.executable, str(ROOT / "tools" / "fake_l86.py"), "--transport", "tcp",
                          "--speed", str(SPEED), "--start", START, *args],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    line = p.stdout.readline()
    assert line.startswith("PORT "), line
    return p, line.split()[1]


@pytest.fixture
def fast_cfg(cfg):
    c = copy.deepcopy(cfg)
    c["live"]["reconnect_s"] = 0.1
    return c


def gnss_s(v):
    return (datetime.fromisoformat(v["t_gnss"].replace("Z", "+00:00")) - T0).total_seconds()


def test_live_sudden_jam_alarm_within_3s(tmp_path, fast_cfg):
    p, port = start_sim("--duration-s", "120", "--jam", "sudden", "--jam-at", "100", "--inject-garbage", "30")
    try:
        s = run_live(port, 9600, tmp_path, fast_cfg, clock_scale=SPEED, duration_s=120 / SPEED + 1.5)
    finally:
        p.kill()
        p.wait(timeout=10)
    eps = read_jsonl(tmp_path / "epochs.jsonl")
    assert len(eps) >= 110
    alarm = next(v for v in eps if v["jamming"]["level"] == "ALARM")
    assert 100 <= gnss_s(alarm) <= 103, alarm["t_gnss"]
    assert all(v["spoofing"]["level"] == "CLEAR" for v in eps)
    kinds = [e["kind"] for e in read_jsonl(tmp_path / "events.jsonl")]
    for k in ("serial_open", "antenna", "warmup_done", "home_set", "level"):
        assert k in kinds, k
    assert s["first_alarm"]["jamming"] == alarm["t_gnss"]
    assert list((tmp_path / "raw").glob("nmea_*.nmea")) and (tmp_path / "latest.json").exists()


def test_live_reconnect_and_galileo_after_power_loss(tmp_path, fast_cfg):
    p, port = start_sim("--duration-s", "100", "--inject-disconnect", "50:3")
    try:
        run_live(port, 9600, tmp_path, fast_cfg, constellations=["gps", "glonass", "galileo"],
                 clock_scale=SPEED, duration_s=100 / SPEED + 1.5)
    finally:
        p.kill()
        p.wait(timeout=10)
    ev = read_jsonl(tmp_path / "events.jsonl")
    opens = [e for e in ev if e["kind"] == "serial_open"]
    assert len(opens) >= 2 and opens[-1]["reconnect"]                       # the module lost power and came back
    assert any(e["kind"] == "serial_lost" for e in ev)
    assert sum(e["kind"] == "pmtk_ack" and e["command"] == "353" and e["status"] == "ok" for e in ev) >= 2
    assert any(e["kind"] == "galileo_confirmed" for e in ev)                # PMTK353 re-sent after the reset
    eps = read_jsonl(tmp_path / "epochs.jsonl")
    assert any(k.startswith("E") for k in eps[-1]["sats"])
    assert all(v["jamming"]["level"] != "ALARM" and v["spoofing"]["level"] != "ALARM" for v in eps)


def test_pmtk353_command():
    assert pmtk353(["gps", "glonass", "galileo"]) == "PMTK353,1,1,1,0,0"
    with pytest.raises(ValueError):
        pmtk353(["gps", "navic"])
