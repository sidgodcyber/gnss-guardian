"""Every simulator profile through the whole detector (NMEA -> epochs -> verdicts), offline.

Latency = GNSS seconds from the profile's start (t = 120 s, after the 60 s warm-up) to the first
ALARM epoch. Requirement: <= 3 s, raised by the right detector, the other detector never in ALARM.
The rig walks a 20 x 10 m loop at 1.2 m/s throughout.
"""
import random

import pytest
from conftest import fake_l86, first_level, run_engine, sim_file

ONSET = 120
WALK = "loop:20:10:1.2"


@pytest.mark.parametrize("args,detector,other", [
    (["--jam", "sudden"], "jamming", "spoofing"),
    (["--jam", "ramp"], "jamming", "spoofing"),               # 4 dB/s: 6 dB crossed at +1.5 s
    (["--spoof", "jump"], "spoofing", "jamming"),
    (["--spoof", "drift"], "spoofing", "jamming"),            # 1 m/s drag-off, spoofer's uniform C/N0 sky
    (["--spoof", "time"], "spoofing", "jamming"),
], ids=["jam-sudden", "jam-ramp", "spoof-jump", "spoof-drift", "spoof-time"])
def test_profile_detected_within_3s(tmp_path, cfg, args, detector, other):
    v = run_engine(sim_file(tmp_path / "p.nmea", *args, duration_s=200).read_text().splitlines(), cfg)
    assert first_level(v[:ONSET], detector, "SUSPECT") is None              # quiet before the attack
    t = first_level(v, detector)
    assert t is not None and 0 <= t - ONSET <= 3, f"{detector} ALARM at {t}"
    assert first_level(v, other) is None
    alarm = next(x for x in v if x[detector]["level"] == "ALARM")
    assert alarm[detector]["reasons"] and not alarm["gnss_trusted"]


@pytest.mark.parametrize("args,detector", [(["--jam", "sudden"], "jamming"), (["--spoof", "time"], "spoofing")],
                         ids=["jam", "spoof"])
def test_recovery_returns_to_clear(tmp_path, cfg, args, detector):
    v = run_engine(sim_file(tmp_path / "r.nmea", *args, "--recover-after", "40", duration_s=260)
                   .read_text().splitlines(), cfg)
    assert first_level(v, detector) is not None
    tail = v[-30:]
    assert all(x[detector]["level"] == "CLEAR" and x["gnss_trusted"] for x in tail)
    assert first_level(v, "jamming" if detector == "spoofing" else "spoofing") is None


def test_natural_drift_needs_vision(tmp_path, cfg):
    """A 1 m/s drag-off with the real satellites and C/N0 is invisible to GNSS-only indicators
    (documented limit); the vision velocity hook catches it. Vision = true walk velocity + 0.2 m/s noise."""
    lines = sim_file(tmp_path / "d.nmea", "--spoof", "drift", "--spoof-cn0", "natural", duration_s=200) \
        .read_text().splitlines()
    assert first_level(run_engine(lines, cfg), "spoofing", "SUSPECT") is None
    rng = random.Random(3)

    def vision(ep):
        s = (ep.t_gnss - fake_l86.datetime(2026, 10, 22, 14, 0, tzinfo=fake_l86.timezone.utc)).total_seconds()
        _, _, ve, vn = fake_l86.walk_at(WALK, s)
        return ve + rng.gauss(0, 0.2), vn + rng.gauss(0, 0.2), 0.9

    t = first_level(run_engine(lines, cfg, vision), "spoofing")
    assert t is not None and t - ONSET <= 5, t
    clean = sim_file(tmp_path / "c.nmea", duration_s=200).read_text().splitlines()
    assert first_level(run_engine(clean, cfg, vision), "spoofing", "SUSPECT") is None
