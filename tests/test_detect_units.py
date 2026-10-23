"""Baseline, hysteresis, jamming and spoofing scores on hand-made features and epochs."""
import math

import pytest
from conftest import at

from gnss_guardian.detect.features import FeatureExtractor
from gnss_guardian.detect.hysteresis import ALARM, CLEAR, SUSPECT, Hysteresis
from gnss_guardian.detect.jamming import JammingDetector
from gnss_guardian.detect.spoofing import SpoofingDetector
from gnss_guardian.io.epochs import Epoch, Sat


def epoch(k, cn0s, fix=True, lat=29.2556, lon=79.537, t_off=0.0, hdop=0.9):
    sats = {key: Sat(c, 45.0, 0.0, True) for key, c in cn0s.items()}
    return Epoch(seq=k, t_gnss=at(k + t_off), t_host=at(k), mono=1000.0 + k, fix_valid=fix,
                 lat=lat if fix else None, lon=lon if fix else None, alt_m=500.0 if fix else None,
                 sog_mps=0.0 if fix else None, cog_deg=0.0 if fix else None, hdop=hdop if fix else None,
                 sats_used=len(cn0s), sats=sats, sats_new=True)


SKY = {f"G{p:02d}": c for p, c in zip(range(1, 13), (44, 41, 38, 36, 35, 33, 31, 40, 42, 37, 22, 34))}


def warm(cfg, n=70):
    fx = FeatureExtractor(cfg)
    for k in range(n):
        e = epoch(k, {s: c + (k % 3) - 1 for s, c in SKY.items()})
        fx.commit(e, fx.compute(e), clean=True)
    return fx


def features(fx, e):
    return fx.compute(e)


def test_baseline_warmup_min_samples_and_freeze(cfg):
    fx = FeatureExtractor(cfg)
    for k in range(15):
        e = epoch(k, SKY)
        fx.commit(e, fx.compute(e), clean=True)
    assert not fx.baseline.ready and fx.baseline.sat_medians() == {}      # < min_samples
    fx = warm(cfg)
    assert fx.baseline.ready and len(fx.baseline.sat_medians()) == 11      # G11 at 22 dB-Hz: too weak to judge
    before = {k: len(v) for k, v in fx.baseline.sats.items()}
    for k in range(70, 400):                                               # a long attack: nothing expires
        e = epoch(k, {s: c - 20 for s, c in SKY.items()}, fix=False)
        fx.commit(e, fx.compute(e), clean=False)
    assert {k: len(v) for k, v in fx.baseline.sats.items()} == before


def test_lost_satellites_count_as_dropped_and_returners_are_not_newcomers(cfg):
    fx = warm(cfg)
    sky = {s: c for s, c in SKY.items() if s not in ("G01", "G02", "G03", "G04")}
    f = features(fx, epoch(70, sky))
    assert f["n_lost"] == 4 and f["n_dropped"] == 4 and f["mean_drop_db"] > 5
    f = features(fx, epoch(71, {**SKY, "G20": 45, "G21": 45}))
    assert f["n_newcomers"] == 2 and math.isclose(f["newcomer_frac"], 2 / 14)


def test_hysteresis_entry_no_flicker_and_hold(cfg):
    h = cfg["detect"]["hysteresis"]
    hy = Hysteresis(h)
    assert hy.update(0.9, 0)[0] == ALARM                                   # immediate
    hy = Hysteresis(h)
    levels = [hy.update(s, t)[0] for t, s in enumerate([0.65, 0.55, 0.65, 0.55, 0.65, 0.55])]
    assert ALARM not in levels and levels.count(SUSPECT) == 6              # k=2 consecutive never reached, no flicker
    assert hy.update(0.65, 6)[0] == SUSPECT and hy.update(0.65, 7)[0] == ALARM
    out = [hy.update(0.1, 8 + t)[0] for t in range(12)]
    assert out[:5] == [ALARM] * 5 and out[5] == SUSPECT and out[-1] == CLEAR
    hy = Hysteresis(h, min_alarm_s=30)
    hy.update(0.9, 0)
    assert [hy.update(0.0, t)[0] for t in range(1, 30)].count(ALARM) == 29  # latched


def test_jamming_broadband_vs_partial_blockage_vs_sky_change(cfg):
    fx, jd = warm(cfg), JammingDetector(cfg)
    s = jd.score(features(fx, epoch(70, {k: c - 20 for k, c in SKY.items() if c - 20 >= 12}, fix=False)))
    assert s.value >= 0.9 and "fix lost" in s.reasons and any("satellites" in r for r in s.reasons)
    partial = dict(SKY, G01=29, G02=26, G03=23)                           # three satellites behind a body/pole
    assert jd.score(features(fx, epoch(71, partial))).value < cfg["detect"]["hysteresis"]["suspect_on"]
    half = {k: c for i, (k, c) in enumerate(SKY.items()) if i % 2}        # half the sky gone, the rest unchanged
    s = jd.score(features(fx, epoch(72, half)))
    assert s.value < cfg["detect"]["hysteresis"]["suspect_on"] and any("sky change" in n for n in s.notes)
    s = jd.score(features(fx, epoch(73, SKY, fix=False)))
    assert s.value <= 0.1 and any("C/N0 normal" in n for n in s.notes)


@pytest.mark.parametrize("case", ["jump", "time", "altitude", "uniform_newcomers"])
def test_spoofing_indicators(cfg, case):
    fx, sd = warm(cfg), SpoofingDetector(cfg)
    if case == "jump":
        e = epoch(70, SKY, lat=29.2556 + 0.0045)                          # 500 m north in 1 s
    elif case == "time":
        e = epoch(70, SKY, t_off=30)
    elif case == "altitude":
        e = epoch(70, SKY)
        e.alt_m = 560.0
    else:
        e = epoch(70, {**{k: 48 for k in list(SKY)[:6]}, **{f"G{p}": 48 for p in range(20, 26)}})
    s = sd.score(features(fx, e))
    want = {"jump": "position jumped", "time": "GNSS time jumped", "altitude": "altitude",
            "uniform_newcomers": "uniform"}[case]
    assert any(want in r for r in s.reasons), s.reasons
    assert s.value >= (0.6 if case in ("altitude", "uniform_newcomers") else 0.9)
    if case == "jump":                                                    # under jamming the jump is not blamed
        assert sd.score(features(fx, e), jammed=True).value == 0
