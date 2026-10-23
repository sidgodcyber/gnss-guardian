"""Simulator: --write output format and receiver model, PMTK353 handling."""
from conftest import fake_l86, sim_file

from gnss_guardian.io.nmea import checksum_ok, split_prefix


def test_write_format_and_jam_fix_loss(tmp_path):
    lines = sim_file(tmp_path / "j.nmea", "--jam", "sudden", "--jam-at", "10", duration_s=20).read_text().splitlines()
    parsed = [split_prefix(x) for x in lines]
    assert all(fmt == "iso_prefix" and checksum_ok(s) for fmt, _, s in parsed)
    assert parsed[0][2].startswith("$GPTXT,01,01,02,ANTSTATUS=OK")
    rmc = [s for _, _, s in parsed if s[3:6] == "RMC"]
    assert len(rmc) == 20 and rmc[9].split(",")[2] == "A" and rmc[10].split(",")[2] == "V"
    assert not any(s.startswith("$GAGSV") for _, _, s in parsed)            # Galileo off at power-on


def test_pmtk353_enables_galileo_and_power_loss_resets():
    m = fake_l86.Module(9600, 10)
    acks = m.command((fake_l86.sentence("PMTK353,1,1,1,0,0") + "\r\n").encode())
    assert m.galileo and acks == [(fake_l86.sentence("PMTK001,353,3") + "\r\n").encode()]
    m.reset()
    assert not m.galileo and m.rate == 1.0
