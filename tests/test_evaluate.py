"""Evaluation harness (gnss_guardian/evaluate.py): one run of two profiles under all five configurations.
Every row is labelled as simulator data; the camera ablation is what catches the natural drift."""
import csv
import json

from gnss_guardian import evaluate


def test_eval_rows_and_labels(tmp_path, cfg):
    rows = evaluate.evaluate(cfg, 1, tmp_path, profiles=["spoof-drift-natural", "jam-sudden"], echo=lambda s: None)
    assert len(rows) == 2 * len(evaluate.CONFIGS)
    assert all(r["source"].startswith("SIMULATOR fake_l86 seed 100") for r in rows)
    by = {(r["profile"], r["config"]): r for r in rows}
    assert by[("spoof-drift-natural", "full")]["detected"] and by[("spoof-drift-natural", "full")]["first_indicator"] == "vision"
    assert not by[("spoof-drift-natural", "no-camera")]["detected"]
    assert "SIMULATED" in by[("spoof-drift-natural", "full")]["source"]
    assert all(by[("jam-sudden", c)]["latency_s"] == 0 for c in evaluate.CONFIGS)
    assert all(r["false_alarm_epochs"] == 0 for r in rows)
    with open(tmp_path / "results.csv", encoding="utf-8") as f:
        assert len(list(csv.DictReader(f))) == len(rows)
    assert "SIMULATOR" in (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert json.loads((tmp_path / "eval.json").read_text())["data"] == "simulator"
