"""AI second opinion (gnss_guardian/detect/ai.py): anomaly model of clean epochs, advisory only.

  export       the IsolationForest exported to JSON scores exactly like sklearn (skipped without sklearn)
  training     Mahalanobis always; the forest kept only if it separates a one-feature outlier, else the
               model says why; Mahalanobis only without scikit-learn; too few epochs refused
  clean        a simulator walk with another seed and route: zero AI SUSPECT
  attack       sudden jam: AI SUSPECT within 3 s with reason codes
  combine      corroboration only raises the spoofing score, only while the rule score >= 0.35; on every
               simulator profile, with the AI each epoch's spoofing score and level are >= without it,
               and the AI never changes gnss_trusted, the state or the latch (advisory)
  runtime      missing model: AI off with a reason and an ai_model event; ai-train / ai-eval CLI
All models here are trained on the simulator: provisional, not real L86.
"""
import copy
import json

import numpy as np
import pytest
from conftest import run_engine, sim_file

from click.testing import CliRunner

from gnss_guardian.cli import cli
from gnss_guardian.config import load_config
from gnss_guardian.detect import ai
from gnss_guardian.run import replay
from gnss_guardian.session import read_jsonl

LABEL = "provisional, not real L86 (simulator, test)"
RANK = {"CLEAR": 0, "SUSPECT": 1, "ALARM": 2}


def base_cfg():
    c = load_config()
    c["detect"]["sky"]["almanac_file"] = None
    c["ai"]["model_file"] = None
    return c


@pytest.fixture(scope="module")
def model_path(tmp_path_factory):
    d = tmp_path_factory.mktemp("ai")
    lines = sim_file(d / "train.nmea", "--seed", "21", duration_s=900).read_text().splitlines()
    X = ai.collect([lines], base_cfg())
    m = ai.train(X, base_cfg(), LABEL, sources=["train.nmea"])
    ai.save(m, d / "model.json")
    return d / "model.json"


def ai_cfg(model_path, **kw):
    c = base_cfg()
    c["ai"]["model_file"] = str(model_path)
    c["ai"].update(kw)
    return c


def test_features_vector():
    f = ai.AIFeatures()
    v = f.vector({"cn0_std": 4.0, "cn0_mean": 40.0, "implied_speed_mps": 1.5, "sog_mps": 1.0, "n_tracked": 10,
                  "n_used": 8, "time_jump_s": None})
    d = dict(zip(ai.FEATURES, v))
    assert d["cn0_std"] == 4.0 and d["speed_mismatch_mps"] == 0.5 and d["used_frac"] == 0.8
    assert np.isnan(d["time_jump_s"]) and np.isnan(d["cn0_step_db"])          # missing: neutral at scoring
    assert dict(zip(ai.FEATURES, f.vector({"cn0_mean": 37.5})))["cn0_step_db"] == 2.5


def test_forest_export_matches_sklearn():
    ens = pytest.importorskip("sklearn.ensemble")
    rng = np.random.default_rng(0)
    Z = rng.normal(size=(500, len(ai.FEATURES)))
    f = ens.IsolationForest(n_estimators=20, max_samples=64, random_state=0).fit(Z)
    Q = np.vstack([Z[:50], rng.normal(scale=4, size=(50, len(ai.FEATURES)))])
    ours = ai._iforest_scores(json.loads(json.dumps(ai.export_forest(f))), Q)
    assert np.allclose(ours, -f.score_samples(Q), atol=1e-9)


def test_training_models(model_path, monkeypatch):
    m = json.loads(model_path.read_text())
    assert m["format"] == ai.FORMAT and m["label"] == LABEL and m["trained"]["epochs"] >= 700
    assert m["mahalanobis"]["calib"]["full"] > m["mahalanobis"]["calib"]["q"]
    assert m["iforest"] is not None or "iforest_rejected" in m["trained"]     # kept, or the reason why not
    print("forest:", "kept" if m["iforest"] else m["trained"]["iforest_rejected"])
    X = np.random.default_rng(1).normal(size=(400, len(ai.FEATURES)))
    monkeypatch.setitem(__import__("sys").modules, "sklearn", None)           # no scikit-learn (as on the Pi)
    mh = ai.train(X, base_cfg(), "x")
    assert mh["iforest"] is None and mh["trained"]["sklearn"] is None
    assert ai.AIModel(mh).backend == "mahalanobis"
    with pytest.raises(ValueError, match="clean epochs"):
        ai.train(X[:10], base_cfg(), "x")


def levels(v, key="level"):
    return [x["ai"][key] for x in v if x["ai"] is not None]


def test_clean_walk_no_ai_suspect(tmp_path, model_path):
    lines = sim_file(tmp_path / "c.nmea", "--seed", "22", duration_s=1200, walk="loop:30:15:1.0").read_text()
    v = run_engine(lines.splitlines(), ai_cfg(model_path))
    warm = [x for x in v if not x["warming_up"]]
    print("clean: AI score max", max(x["ai"]["score"] for x in warm), "over", len(warm), "epochs")
    assert len(warm) > 1000 and all(x["ai"]["level"] == "CLEAR" for x in warm)
    assert all(x["ai"]["model"] == LABEL for x in warm)


def test_jam_raises_ai_suspect(tmp_path, model_path):
    v = run_engine(sim_file(tmp_path / "j.nmea", "--jam", "sudden", "--jam-at", "120", duration_s=200)
                   .read_text().splitlines(), ai_cfg(model_path))
    t0 = v[0]["seq"]
    first = next(x for x in v if x["ai"]["level"] == "SUSPECT")
    print("jam: AI SUSPECT at", first["seq"] - t0, "s:", first["ai"]["reasons"])
    assert 120 <= first["seq"] - t0 <= 123
    assert any(c.endswith("DROP_HIGH") for c in first["ai"]["codes"]) and first["ai"]["reasons"][0].startswith("AI: ")


def test_corroborate_only_raises():
    c = base_cfg()
    for rule in np.linspace(0, 1, 21):
        for s in np.linspace(0, 1, 11):
            out = ai.corroborate(float(rule), {"score": float(s)}, c)
            assert out >= rule
            if rule < c["ai"]["boost_min_rule"] or s == 0:
                assert out == rule
    assert ai.corroborate(0.5, {"score": 1.0}, c) == pytest.approx(1 - 0.5 * 0.5)
    assert ai.corroborate(0.5, None, c) == 0.5


@pytest.mark.parametrize("args", [[], ["--jam", "sudden", "--recover-after", "40"], ["--jam", "ramp"],
                                  ["--spoof", "jump", "--recover-after", "40"], ["--spoof", "drift"],
                                  ["--spoof", "time", "--recover-after", "40"]],
                         ids=["clean", "jam-sudden", "jam-ramp", "spoof-jump", "spoof-drift", "spoof-time"])
def test_never_lowers_and_advisory(tmp_path, model_path, args):
    lines = sim_file(tmp_path / "p.nmea", *args, duration_s=260).read_text().splitlines()
    on, off = run_engine(lines, ai_cfg(model_path)), run_engine(lines, base_cfg())
    assert len(on) == len(off)
    for a, b in zip(on, off):
        assert a["spoofing"]["score"] >= b["spoofing"]["score"] - 1e-9, a["seq"]
        assert RANK[a["spoofing"]["level"]] >= RANK[b["spoofing"]["level"]], a["seq"]
        assert a["jamming"] == b["jamming"]                                    # the boost is spoofing only
        if "ai_corroboration" in a["spoofing"]["parts"]:
            assert b["spoofing"]["score"] >= 0.35
    # advisory: an always-alarmed AI on a clean walk changes nothing but its own fields and the score
    s = replay(sim_file(tmp_path / "c.nmea", duration_s=200), ai_cfg(model_path, suspect_on=0.0, suspect_off=-1),
               tmp_path / "s")
    eps = read_jsonl(tmp_path / "s" / "epochs.jsonl")
    assert s["states"] == ["WARMING_UP", "GNSS_OK"]
    warm = [x for x in eps if not x["warming_up"]]
    assert all(x["ai"]["level"] == "SUSPECT" for x in warm[3:]) and all(x["gnss_trusted"] for x in warm)
    assert all(x["spoofing"]["level"] == "CLEAR" for x in warm)


def test_missing_model_and_cli(tmp_path, model_path):
    a = ai.AISecondOpinion(ai_cfg(tmp_path / "nope.json"))
    assert not a.enabled and "not found" in a.status["reason"] and a.update({}, False) is None
    s = replay(sim_file(tmp_path / "w.nmea", duration_s=120), ai_cfg(tmp_path / "nope.json"), tmp_path / "s0")
    ev = [e for e in read_jsonl(tmp_path / "s0" / "events.jsonl") if e["kind"] == "ai_model"]
    assert ev and not ev[0]["enabled"] and s["epochs"] > 100
    replay(sim_file(tmp_path / "w2.nmea", duration_s=120), ai_cfg(model_path), tmp_path / "s1")
    ev = [e for e in read_jsonl(tmp_path / "s1" / "events.jsonl") if e["kind"] == "ai_model"]
    assert ev[0]["enabled"] and ev[0]["label"] == LABEL
    d = json.loads((tmp_path / "s1" / "dashboard.json").read_text(encoding="utf-8"))
    assert any(r["ai"] and r["ai"]["model"] == LABEL for r in d["timeline"])

    train = sim_file(tmp_path / "t.nmea", "--seed", "23", duration_s=600)
    cfg_file = tmp_path / "c.yaml"
    c = base_cfg()
    import yaml
    cfg_file.write_text(yaml.safe_dump(c))
    r = CliRunner().invoke(cli, ["ai-train", str(train), "--label", "provisional, cli test", "--out",
                                 str(tmp_path / "m.json"), "--skip-min", "1", "--config", str(cfg_file)])
    assert r.exit_code == 0, r.output
    assert "provisional, cli test" in r.output and json.loads((tmp_path / "m.json").read_text())["trained"]["epochs"] > 400
    r = CliRunner().invoke(cli, ["ai-eval", str(train), "--model", str(tmp_path / "m.json"), "--config", str(cfg_file)])
    assert r.exit_code == 0 and "AI SUSPECT 0 epochs" in r.output, r.output
