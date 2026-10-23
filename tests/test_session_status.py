"""Session folder from a replay (session.json + home, epochs, events, latest), and the status line."""
import json
import os
import time

import pytest
from conftest import sim_file

from gnss_guardian.home import parse_home
from gnss_guardian.run import replay
from gnss_guardian.session import read_jsonl
from gnss_guardian.status import status_line


def test_replay_session_files_and_home(tmp_path, cfg):
    src = sim_file(tmp_path / "s.nmea", "--jam", "sudden", "--jam-at", "120", walk=None, duration_s=150)
    echo = []
    s = replay(src, cfg, tmp_path / "auto", echo=echo.append)
    info = json.loads((tmp_path / "auto" / "session.json").read_text())
    home = info["home"]
    assert home["source"].startswith("median of the first 30 s") and abs(home["lat"] - 29.2556) < 1e-4
    eps = read_jsonl(tmp_path / "auto" / "epochs.jsonl")
    assert len(eps) == s["epochs"] == 150
    assert eps[110]["enu_m"] is not None and eps[-1]["enu_m"] is None and json.loads((tmp_path / "auto" / "latest.json").read_text())["seq"] == 149
    ev = read_jsonl(tmp_path / "auto" / "events.jsonl")
    assert [e["kind"] for e in ev if e["kind"] in ("warmup_done", "home_set")] == ["warmup_done", "home_set"]
    assert any(e["kind"] == "level" and e["level"] == "ALARM" for e in ev)
    assert any("jamming  -> ALARM" in line for line in echo)
    s2 = replay(src, cfg, tmp_path / "given", home=(29.2560, 79.5370))
    assert s2["home"]["source"] == "command line"
    e2 = read_jsonl(tmp_path / "given" / "epochs.jsonl")[100]
    assert abs(e2["enu_m"][1] - (29.2556 - 29.2560) * 111195) < 2           # ~44 m south of the given home
    with pytest.raises(ValueError):
        parse_home("95,10")


def test_status_line_trusted_alarm_and_stale(tmp_path, cfg):
    src = sim_file(tmp_path / "s.nmea", "--jam", "sudden", "--jam-at", "120", walk=None, duration_s=125)
    replay(src, cfg, tmp_path / "jam")
    line, stale = status_line(tmp_path / "jam")
    assert not stale and "GNSS denied: jamming or blockage" in line and "jam ALARM" in line and "fix lost" in line
    old = time.time() - 60
    os.utime(tmp_path / "jam" / "latest.json", (old, old))
    line, stale = status_line(tmp_path / "jam")
    assert stale and line.split(" | ")[1].startswith("STALE")
    assert line.split(" | ")[2] == "GNSS UNKNOWN" and "GNSS denied" not in line and "last: fix lost" in line
    assert status_line(tmp_path / "nothing")[1]


def test_status_line_stale_trusted_epoch_is_unknown(tmp_path, cfg):
    src = sim_file(tmp_path / "s.nmea", walk=None, duration_s=75)
    replay(src, cfg, tmp_path / "clean")
    line, stale = status_line(tmp_path / "clean")
    assert not stale and line.split(" | ")[1] == "GNSS TRUSTED" and "age " in line
    old = time.time() - 6                                                  # just past STALE_S
    os.utime(tmp_path / "clean" / "latest.json", (old, old))
    line, stale = status_line(tmp_path / "clean")
    assert stale and "TRUSTED" not in line and line.split(" | ")[2] == "GNSS UNKNOWN"
    assert "last: fix " in line and "age " not in line
