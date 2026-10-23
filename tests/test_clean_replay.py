"""False alarms on clean data, and the real foil-wrap denial test.

  sim walk     30 min of the simulator (synthetic C/N0, walking loop): zero ALARM, zero SUSPECT
  proxy phone  L86-style NMEA made from real phone GnssLogger logs (tools/make_proxy_fixture.py):
               real C/N0 dynamics and positions, but a phone, not an L86 -> zero ALARM; SUSPECT is reported
  real L86     tests/fixtures/real_l86_clean.nmea (30 min outdoors, tools/record_nmea.py) -> zero ALARM
               tests/fixtures/real_l86_foil.nmea (antenna wrapped in foil) -> jamming ALARM within 3 s of the
               C/N0 collapse. Both skip until the recordings exist.
"""
import statistics

import pytest
from conftest import FIX, run_engine, sim_file

from gnss_guardian.io.epochs import epochs_from_lines

REAL_CLEAN = FIX / "real_l86_clean.nmea"
REAL_FOIL = FIX / "real_l86_foil.nmea"


def levels(verdicts, d):
    return {lvl: sum(v[d]["level"] == lvl for v in verdicts) for lvl in ("SUSPECT", "ALARM")}


def test_sim_walk_30min_no_alarms(tmp_path, cfg):
    v = run_engine(sim_file(tmp_path / "clean.nmea", duration_s=1800).read_text().splitlines(), cfg)
    assert len(v) == 1800
    for d in ("jamming", "spoofing"):
        assert levels(v, d) == {"SUSPECT": 0, "ALARM": 0}, d
    assert sum(x["gnss_trusted"] for x in v) == 1800 - 62                   # all but the 60 s warm-up (62 epochs)


@pytest.mark.parametrize("name", ["proxy_phone_2026_10_18.nmea", "proxy_phone_2026_10_21.nmea"])
def test_phone_proxy_no_alarms(cfg, name):
    v = run_engine((FIX / name).read_text().splitlines(), cfg)
    for d in ("jamming", "spoofing"):
        n = levels(v, d)
        print(f"{name} {d}: {n}")
        assert n["ALARM"] == 0, (d, n)


@pytest.mark.skipif(not REAL_CLEAN.exists(), reason="no real L86 recording yet: record 30 min outdoors with "
                    "tools/record_nmea.py and save it as tests/fixtures/real_l86_clean.nmea")
def test_real_l86_clean_no_alarms(cfg):
    v = run_engine(REAL_CLEAN.read_text(encoding="ascii", errors="replace").splitlines(), cfg)
    assert len(v) >= 25 * 60
    for d in ("jamming", "spoofing"):
        n = levels(v, d)
        print(f"real clean {d}: {n}")
        assert n["ALARM"] == 0, (d, n)


def collapse_epoch(epochs, ref_s=60, drop_db=6.0):
    """Independent of the detector: the first epoch after the first ref_s seconds whose mean C/N0
    over the reference satellites (tracked in the first ref_s s, lost counted as 0) falls drop_db
    below its reference median."""
    t0 = epochs[0].mono
    ref = [e for e in epochs if e.mono - t0 < ref_s]
    sats = {k for e in ref for k in e.tracked()}
    def metric(e):
        tr = e.tracked()
        return statistics.fmean(tr[k].cn0 if k in tr else 0.0 for k in sats)
    base = statistics.median(metric(e) for e in ref)
    return next(e for e in epochs[len(ref):] if metric(e) <= base - drop_db)


@pytest.mark.skipif(not REAL_FOIL.exists(), reason="no real foil recording yet: 90 s clean, 60 s antenna wrapped "
                    "in foil, then unwrapped; save as tests/fixtures/real_l86_foil.nmea")
def test_real_l86_foil_jammed_within_3s(cfg):
    lines = REAL_FOIL.read_text(encoding="ascii", errors="replace").splitlines()
    epochs = epochs_from_lines(lines, cfg)
    c = collapse_epoch(epochs)
    v = run_engine(lines, cfg)
    alarm = next((x for x in v if x["jamming"]["level"] == "ALARM"), None)
    assert alarm is not None, "foil wrap never raised the jamming alarm"
    latency = alarm["mono"] - c.mono
    print(f"foil: C/N0 collapse at {c.t_gnss}, ALARM {latency:.1f} s later: {alarm['jamming']['reasons']}")
    assert 0 <= latency <= 3
    assert "GNSS denied: jamming or blockage" in alarm["labels"]
    assert all(x["spoofing"]["level"] != "ALARM" for x in v)
