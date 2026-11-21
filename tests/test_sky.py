"""Sky-geometry check (gnss_guardian/detect/sky.py): GSV az/el against a GPS YUMA almanac.

  almanac model   predicted az/el match the real phone sky of 18 Oct (GnssLogger proxy) within 2 deg
  clean           30 min simulator walk with the almanac sky (--sky), check ON: zero SUSPECT / ALARM;
                  both phone proxies with their week's almanac: zero spoofing SUSPECT / ALARM
  wrong sky       --spoof sky (azimuths rotated 30 deg, 2 below-horizon PRNs, natural C/N0, natural 1 m/s
                  drift): no GNSS-only indicator fires with the check OFF; with it ON, ALARM within 3 s.
                  Ghosts alone: 1 -> SUSPECT, 2 -> ALARM
  time spoof      the sky is predicted on the host clock: a GNSS time jump does not move it
  off             missing file / almanac too old for the data: check off with a reason, verdicts unchanged
Fixtures: tests/fixtures/yuma_2026_291/293/295.alm (NAVCEN archive, 18/20/22 Oct 2026, GPS week 393).
"""
import copy
import random
import statistics

import pytest
from conftest import FIX, T0, fake_l86, first_level, run_engine, sim_file

from gnss_guardian.detect import sky
from gnss_guardian.detect.sky import SkyCheck
from gnss_guardian.io.epochs import epochs_from_lines
from gnss_guardian.run import replay
from gnss_guardian.session import read_jsonl

ALM = FIX / "yuma_2026_295.alm"                  # week of the simulator's start, 2026-10-22
ONSET = 120


def sky_cfg(cfg, path=ALM, enabled=True):
    c = copy.deepcopy(cfg)
    c["detect"]["sky"].update(almanac_file=str(path) if path else None, enabled=enabled)
    return c


def sky_sim(tmp_path, name, *args, duration_s=200):
    return sim_file(tmp_path / name, "--sky", f"almanac:{ALM}", *args, duration_s=duration_s).read_text().splitlines()


def test_parse_yuma():
    alm = sky.parse_yuma(FIX / "yuma_2026_291.alm")
    assert len(alm) == 32 and all(a.week == 393 and 5150 < a.sqrt_a < 5160 for a in alm.values())
    assert sky.full_week(393, 2441) == 2441 and sky.full_week(393, 2440) == 2441 and sky.full_week(1000, 2441) == 2024


def test_almanac_matches_real_phone_sky(cfg):
    """Real GSV (phone, 18 Oct 2026, integer degrees) against the almanac prediction at the phone's fix."""
    alm = sky.parse_yuma(FIX / "yuma_2026_291.alm")
    errs = []
    for ep in epochs_from_lines((FIX / "proxy_phone_2026_10_18.nmea").read_text().splitlines(), cfg)[::5]:
        if not ep.fix_valid:
            continue
        pred = sky.predict(alm, ep.t_gnss, ep.lat, ep.lon, ep.alt_m or 0.0)
        errs += [sky.angle_deg(s.elev, s.az, *pred[int(k[1:])]) for k, s in ep.sats.items()
                 if k.startswith("G") and int(k[1:]) in pred and s.elev == s.elev]
    print(f"phone vs almanac: {len(errs)} directions, median {statistics.median(errs):.2f}, max {max(errs):.2f} deg")
    assert len(errs) > 500 and statistics.median(errs) < 1.0 and max(errs) < 2.0


def test_clean_walk_with_real_sky_no_alarms(tmp_path, cfg):
    v = run_engine(sky_sim(tmp_path, "clean.nmea", duration_s=1800), sky_cfg(cfg))
    assert len(v) == 1800
    for d in ("jamming", "spoofing"):
        assert {x: sum(e[d]["level"] == x for e in v) for x in ("SUSPECT", "ALARM")} == {"SUSPECT": 0, "ALARM": 0}, d
    assert sum(x["gnss_trusted"] for x in v) == 1800 - 62
    errs = [x["features"]["sky_err_deg"] for x in v if x["features"]["sky_err_deg"] is not None]
    assert len(errs) > 1600 and max(errs) < 2.0


@pytest.mark.parametrize("name,alm", [("proxy_phone_2026_10_18.nmea", "yuma_2026_291.alm"),
                                      ("proxy_phone_2026_10_21.nmea", "yuma_2026_293.alm")])
def test_phone_proxy_sky_on_no_spoofing(cfg, name, alm):
    v = run_engine((FIX / name).read_text().splitlines(), sky_cfg(cfg, FIX / alm))
    assert sum(x["features"]["sky_err_deg"] is not None for x in v) > 250
    assert all(x["spoofing"]["level"] == "CLEAR" for x in v)
    assert all((x["features"]["sky_impossible"] or 0) == 0 for x in v)


def test_wrong_sky_caught_without_camera(tmp_path, cfg):
    lines = sky_sim(tmp_path, "sky.nmea", "--spoof", "sky")
    off = run_engine(lines, sky_cfg(cfg, enabled=False))
    assert first_level(off, "spoofing", "SUSPECT") is None          # today: invisible to GNSS-only checks
    v = run_engine(lines, sky_cfg(cfg))
    assert first_level(v[:ONSET], "spoofing", "SUSPECT") is None
    t = first_level(v, "spoofing")
    assert t is not None and 0 <= t - ONSET <= 3, t
    assert first_level(v, "jamming", "SUSPECT") is None
    alarm = next(x for x in v if x["spoofing"]["level"] == "ALARM")
    assert any("off the almanac sky" in r for r in alarm["spoofing"]["reasons"])
    assert any("below the horizon" in r for r in alarm["spoofing"]["reasons"])
    assert not alarm["gnss_trusted"]


def test_wrong_sky_drift_caught_by_camera_when_check_off(tmp_path, cfg):
    """The same profile with the sky check off: only the camera velocity cross-check sees its drift."""
    lines = sky_sim(tmp_path, "skyc.nmea", "--spoof", "sky")
    rng = random.Random(3)

    def vision(ep):
        s = (ep.t_gnss - T0).total_seconds()
        _, _, ve, vn = fake_l86.walk_at("loop:20:10:1.2", s)
        return ve + rng.gauss(0, 0.2), vn + rng.gauss(0, 0.2), 0.9
    t = first_level(run_engine(lines, sky_cfg(cfg, enabled=False), vision), "spoofing")
    assert t is not None and t - ONSET <= 5, t


@pytest.mark.parametrize("ghosts,level", [(1, "SUSPECT"), (2, "ALARM")])
def test_ghost_satellites(tmp_path, cfg, ghosts, level):
    v = run_engine(sky_sim(tmp_path, f"g{ghosts}.nmea", "--spoof", "sky", "--sky-rot-deg", "0", "--sky-ghosts",
                           str(ghosts)), sky_cfg(cfg))
    after = v[ONSET + 1:]
    assert max(x["features"]["sky_impossible"] or 0 for x in after) == ghosts
    assert first_level(v, "spoofing", level) is not None
    if level == "SUSPECT":
        assert first_level(v, "spoofing") is None


def test_time_spoof_does_not_move_predicted_sky(tmp_path, cfg):
    v = run_engine(sky_sim(tmp_path, "t.nmea", "--spoof", "time", "--spoof-cn0", "natural"), sky_cfg(cfg))
    errs = [x["features"]["sky_err_deg"] for x in v[ONSET:] if x["features"]["sky_err_deg"] is not None]
    assert errs and max(errs) < 2.0                    # +30 s GNSS time, sky still predicted on the host clock
    assert first_level(v, "spoofing") is not None        # the time indicator still raises the alarm


def test_missing_file_disables(cfg, tmp_path):
    chk = SkyCheck(sky_cfg(cfg, tmp_path / "nope.alm"))
    assert not chk.enabled and "not found" in chk.status["reason"]
    assert not SkyCheck(sky_cfg(cfg, None)).enabled
    assert not SkyCheck(sky_cfg(cfg, enabled=False)).enabled


def test_stale_almanac_disables_with_event(tmp_path, cfg):
    """A 22 Oct almanac against data 40 days later: the check turns off on the first epoch, with an event,
    and the verdicts equal a run without the check."""
    late = "2026-12-01T14:00:00Z"
    a = fake_l86.build_parser().parse_args(["--write", str(tmp_path / "late.nmea"), "--start", late,
                                            "--duration-s", "150", "--walk", "loop:20:10:1.2"])
    fake_l86.write_file(a, fake_l86.load_cycles(FIX / "l86_sim_10min.nmea"),
                        fake_l86.datetime(2026, 12, 1, 14, tzinfo=fake_l86.timezone.utc))
    s = replay(tmp_path / "late.nmea", sky_cfg(cfg), tmp_path / "sess")
    ev = [e for e in read_jsonl(tmp_path / "sess" / "events.jsonl") if e["kind"] == "sky_check"]
    assert ev[0]["enabled"] and not ev[-1]["enabled"] and "days from the data" in ev[-1]["detail"]
    assert s["spoofing_suspect_epochs"] == 0 and s["spoofing_alarm_epochs"] == 0
    with_check = read_jsonl(tmp_path / "sess" / "epochs.jsonl")
    without = run_engine((tmp_path / "late.nmea").read_text().splitlines(), sky_cfg(cfg, enabled=False))
    assert [x["spoofing"]["score"] for x in with_check] == [x["spoofing"]["score"] for x in without]
