"""Laptop recording path: the real L86 through an Arduino Uno used as a USB-serial bridge (RESET to GND,
L86 TX -> Uno pin 1) on a Windows COM port. Nothing can be sent to the L86, so it stays at its defaults:
1 Hz, 9600 baud, GPS + GLONASS, combined fixes as $GNRMC / $GNGGA, no PMTK replies.

  recorder     tools/record_nmea.py records a serial stream (socket:// stands in for COM5) with host-time
               prefixes, and keeps retrying a COM port that is not there yet
  as-is        a file in that default format (simulator with the almanac sky, RMC/GGA as GN) goes through
               sky-report, ai-train --first-min, ai-eval --skip-min and replay unchanged: zero alarms,
               the sky judged, the model trained and labelled
"""
import json
import subprocess
import sys
from pathlib import Path

import yaml
from conftest import FIX, sim_file
from test_live_sim import start_sim

from click.testing import CliRunner

from gnss_guardian.cli import cli
from gnss_guardian.config import load_config

ROOT = Path(__file__).resolve().parents[1]
ALM = FIX / "yuma_2026_295.alm"


def nmea(body):
    x = 0
    for ch in body:
        x ^= ord(ch)
    return f"${body}*{x:02X}"


def l86_default(lines):
    """As the L86 sends with GLONASS on and no PMTK: combined fixes on the GN talker."""
    out = []
    for ln in lines:
        pre, _, s = ln.partition(",$")
        if s.startswith(("GPRMC", "GPGGA")):
            s = nmea("GN" + s[2:s.rindex("*")])[1:]
        out.append(f"{pre},${s}")
    return out


def test_record_nmea_from_serial_and_missing_com_port(tmp_path):
    p, port = start_sim("--duration-s", "120", "--speed", "1", "--sky", f"almanac:{ALM}")
    try:
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "record_nmea.py"), "--out", str(tmp_path / "r.nmea"),
                            "--port", port, "--baud", "9600", "--duration-min", str(6 / 60), "--status-every-s", "2"],
                           capture_output=True, text=True, timeout=60)
    finally:
        p.kill()
        p.wait(timeout=10)
    assert r.returncode == 0, r.stdout + r.stderr
    lines = (tmp_path / "r.nmea").read_text().splitlines()
    assert len(lines) > 30 and all(ln[:4] == "2026" or ln[:2] == "20" for ln in lines)
    assert all(",$" in ln for ln in lines) and any("RMC" in ln for ln in lines)
    # a COM port that is not there (bridge unplugged): keeps retrying, exits 1 with no lines, no crash
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "record_nmea.py"), "--out", str(tmp_path / "none.nmea"),
                        "--port", "COM250", "--duration-min", str(5 / 60)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "cannot open COM250" in r.stdout, r.stdout + r.stderr


def test_default_l86_file_through_the_tools(tmp_path):
    src = sim_file(tmp_path / "sim.nmea", "--sky", f"almanac:{ALM}", "--seed", "31", duration_s=12 * 60)
    rec = tmp_path / "l86_bridge.nmea"
    rec.write_text("\n".join(l86_default(src.read_text().splitlines())) + "\n")
    assert "$GNRMC" in rec.read_text() and "$GPRMC" not in rec.read_text()
    c = load_config()
    c["detect"]["sky"]["almanac_file"] = str(ALM)
    cfg_file = tmp_path / "c.yaml"
    cfg_file.write_text(yaml.safe_dump(c))
    run = CliRunner()

    r = run.invoke(cli, ["sky-report", str(rec), "--config", str(cfg_file)])
    print(r.output)
    assert r.exit_code == 0, r.output
    assert "sky check: ON" in r.output and "GPS az/el in GSV: yes" in r.output
    assert "spoofing: SUSPECT 0 epochs, ALARM 0 epochs" in r.output and "jamming: SUSPECT 0 epochs, ALARM 0" in r.output

    r = run.invoke(cli, ["ai-train", str(rec), "--first-min", "8", "--label", "bridge test, first 8 of 12 min",
                         "--out", str(tmp_path / "m.json"), "--config", str(cfg_file)])
    print(r.output)
    assert r.exit_code == 0, r.output
    m = json.loads((tmp_path / "m.json").read_text())
    assert 400 <= m["trained"]["epochs"] <= 480 and m["label"] == "bridge test, first 8 of 12 min"
    assert "model chosen on: bridge test, first 8 of 12 min" in r.output

    r = run.invoke(cli, ["ai-eval", str(rec), "--skip-min", "8", "--model", str(tmp_path / "m.json"),
                         "--config", str(cfg_file)])
    print(r.output)
    assert r.exit_code == 0, r.output
    assert "(4.0 min)" in r.output and "spoofing ALARM epochs 0 (rule only 0)" in r.output
