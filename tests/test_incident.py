"""Incident reports (gnss_guardian/incident.py): one report per alarm episode, positions only in the local
report, a demo injection marked as not a real attack, and a hash chain that catches edits to epochs.jsonl
and to the reports."""
import json

from click.testing import CliRunner
from conftest import sim_file

from gnss_guardian import incident
from gnss_guardian.cli import cli
from gnss_guardian.run import replay


def session(tmp_path, cfg, *args):
    replay(sim_file(tmp_path / "p.nmea", *args, "--recover-after", "40", duration_s=260), cfg, tmp_path / "s")
    return tmp_path / "s"


def test_jam_incident_and_verify(tmp_path, cfg):
    s = session(tmp_path, cfg, "--jam", "sudden")
    r = CliRunner().invoke(cli, ["incident-report", str(s)])
    assert r.exit_code == 0 and "1 incident(s)" in r.output, r.output
    d = json.loads((s / "incidents" / "incidents.json").read_text())
    inc = d["incidents"][0]
    assert inc["type"] == "jamming" and inc["state_chain"][0] == "JAMMED" and inc["state_chain"][-1] == "GNSS_OK"
    assert inc["location"]["last_trusted_fix"]["lat"] and inc["duration_s"] > 30 and not inc["demo_injection"]
    assert "NOT verified" in d["field_list_note"] and inc["detector"]["reasons"]
    md = (s / "incidents" / "incident_1.md").read_text(encoding="utf-8")
    assert "jamming or blockage" in md and "Location: last trusted fix" in md
    assert '"lat"' not in (s / "dashboard.json").read_text(encoding="utf-8")      # lat/lon: local report only
    r = CliRunner().invoke(cli, ["incident-verify", str(s)])
    assert r.exit_code == 0 and r.output.startswith("OK"), r.output

    lines = (s / "epochs.jsonl").read_text().splitlines()                         # edit one epoch
    lines[150] = lines[150].replace('"gnss_trusted": false', '"gnss_trusted": true').replace(
        '"gnss_trusted":false', '"gnss_trusted":true')
    assert lines[150] != (s / "epochs.jsonl").read_text().splitlines()[150]
    (s / "epochs.jsonl").write_text("\n".join(lines) + "\n")
    ok, msgs = incident.verify(s)
    assert not ok and "between lines 121 and 180" in msgs[0], msgs


def test_report_edit_detected_and_spoof_type(tmp_path, cfg):
    s = session(tmp_path, cfg, "--spoof", "time")
    r = incident.report(s)
    assert [i["type"] for i in r["incidents"]] == ["spoofing"]
    p = s / "incidents" / "incident_1.md"
    p.write_text(p.read_text(encoding="utf-8").replace("SPOOFING", "NOTHING"), encoding="utf-8")
    ok, msgs = incident.verify(s)
    assert not ok and any("incident_1.md was changed" in m for m in msgs)


def test_demo_injection_marked():
    eps = []
    for i in range(10):
        st = "GNSS_OK" if i < 4 or i > 7 else "SPOOFED"
        eps.append({"seq": i, "mono": float(i), "t_gnss": f"2026-10-22T14:00:0{i}Z", "state": st, "cause": "spoofing",
                    "gnss_trusted": st == "GNSS_OK", "lat": 29.0, "lon": 79.0,
                    "spoofing": {"level": "ALARM" if st != "GNSS_OK" else "CLEAR", "score": 0.9, "reasons": ["x"],
                                 "parts": {"speed": 1.0}}, "jamming": {"level": "CLEAR", "score": 0, "reasons": [], "parts": {}}})
    ev = [{"kind": "demo_inject", "mono": 3.5, "action": "jump", "label": "jump 30 m at 90 deg"},
          {"kind": "demo_inject", "mono": 8.5, "action": "stop"}]
    inc = incident.find_incidents(eps, ev, None)
    assert len(inc) == 1 and inc[0]["demo_injection"] == "jump 30 m at 90 deg" and inc[0]["duration_s"] == 4.0
