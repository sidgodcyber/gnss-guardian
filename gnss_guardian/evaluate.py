"""Evaluation harness (gnss-guardian eval): every attack profile N times plus a clean 30 min run, under an
ablation of the three cross-checks, on the SIMULATOR. Every row says so in its ``source`` column.

    gnss-guardian eval --runs 3 --out eval/          -> eval/results.csv, eval/summary.md, eval/eval.json

Profiles (tools/fake_l86.py, almanac sky from tests/fixtures/yuma_2026_295.alm, walking 20 x 10 m loop,
attack at t = 120 s, 260 s per run, seed = 100 + run):
  jam-sudden, jam-ramp, spoof-jump, spoof-drift (spoofer's uniform C/N0), spoof-time,
  spoof-drift-natural (real C/N0, same satellites: the hard case), spoof-sky (+30 deg azimuths, 2 ghosts,
  1 m/s drift), and clean (1800 s).
Configurations: full (camera + AI + sky), no-camera, no-AI, no-sky, rules-only.
  camera  the detector's camera-velocity hook fed the TRUE walk velocity + 0.2 m/s noise (quality 0.9):
          a simulated camera, not the real vision pipeline
  AI      the model in ai.model_file, with its own label (provisional unless retrained on real L86)
  sky     the sky check with the almanac above
Per run: detected (the expected detector reaches ALARM after the onset), latency to ALARM and to SUSPECT
(simulated seconds), the indicator with the largest share at the first non-CLEAR epoch, false-alarm
epochs (any detector SUSPECT/ALARM before the onset, after warm-up; the whole run for clean), and an
alarm by the wrong detector.
"""
from __future__ import annotations

import copy
import csv
import json
import random
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gnss_guardian.config import ROOT

ONSET = 120
DURATION = 260
CLEAN_S = 1800
START = "2026-10-22T14:00:00Z"
WALK = "loop:20:10:1.2"
ALMANAC = ROOT / "tests" / "fixtures" / "yuma_2026_295.alm"
PROFILES = {
    "jam-sudden": (["--jam", "sudden"], "jamming"),
    "jam-ramp": (["--jam", "ramp"], "jamming"),
    "spoof-jump": (["--spoof", "jump"], "spoofing"),
    "spoof-drift": (["--spoof", "drift"], "spoofing"),
    "spoof-time": (["--spoof", "time"], "spoofing"),
    "spoof-drift-natural": (["--spoof", "drift", "--spoof-cn0", "natural"], "spoofing"),
    "spoof-sky": (["--spoof", "sky"], "spoofing"),
    "clean": ([], None),
}
CONFIGS = {"full": (True, True, True), "no-camera": (False, True, True), "no-AI": (True, False, True),
           "no-sky": (True, True, False), "rules-only": (False, False, False)}
OTHER = {"jamming": "spoofing", "spoofing": "jamming"}


def _load_tool(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sim_lines(fake, path: Path, args: list[str], seed: int, duration: int) -> list[str]:
    argv = ["--write", str(path), "--start", START, "--duration-s", str(duration), "--walk", WALK,
            "--seed", str(seed), "--sky", f"almanac:{ALMANAC}", *args]
    a = fake.build_parser().parse_args(argv)
    t0 = datetime.fromisoformat(START.replace("Z", "+00:00"))
    fake.write_file(a, fake.load_cycles(fake.FIXTURE), t0)
    return path.read_text().splitlines()


def config_for(cfg: dict, camera: bool, ai: bool, sky: bool) -> dict:
    c = copy.deepcopy(cfg)
    c["detect"]["sky"]["almanac_file"] = str(ALMANAC) if sky else None
    c["ai"]["enabled"] = ai
    return c


def run_one(lines, cfg, camera: bool, fake, seed: int) -> list[dict]:
    from gnss_guardian.detect.engine import DetectionEngine
    from gnss_guardian.io.epochs import epochs_from_lines
    eng = DetectionEngine(cfg)
    rng = random.Random(seed)
    t0 = datetime.fromisoformat(START.replace("Z", "+00:00"))
    out = []
    for ep in epochs_from_lines(lines, cfg):
        if camera and ep.mono is not None and ep.t_gnss is not None:
            s = (ep.t_gnss - t0).total_seconds()
            _, _, ve, vn = fake.walk_at(WALK, s)
            eng.observe_vision(ep.mono, ve + rng.gauss(0, 0.2), vn + rng.gauss(0, 0.2), 0.9)
        out.append(eng.update(ep))
    return out


def score_run(v: list[dict], detector: str | None) -> dict:
    t0 = v[0]["seq"]
    rel = [(x["seq"] - t0, x) for x in v]
    before = [x for t, x in rel if (detector is None or t < ONSET) and not x["warming_up"]]
    fa = sum(1 for x in before if x["jamming"]["level"] != "CLEAR" or x["spoofing"]["level"] != "CLEAR")
    r = {"detected": None, "latency_s": None, "suspect_latency_s": None, "first_indicator": None,
         "false_alarm_epochs": fa, "wrong_detector_alarm": None,
         "ai_suspect_epochs": sum(1 for x in v if (x.get("ai") or {}).get("level") == "SUSPECT")}
    if detector is None:
        return r
    after = [(t, x) for t, x in rel if t >= ONSET]
    alarm = next(((t, x) for t, x in after if x[detector]["level"] == "ALARM"), None)
    sus = next(((t, x) for t, x in after if x[detector]["level"] != "CLEAR"), None)
    r["detected"] = alarm is not None
    r["latency_s"] = alarm[0] - ONSET if alarm else None
    r["suspect_latency_s"] = sus[0] - ONSET if sus else None
    if sus:
        parts = sus[1][detector]["parts"]
        r["first_indicator"] = max(parts, key=parts.get) if parts else None
    r["wrong_detector_alarm"] = any(x[OTHER[detector]]["level"] == "ALARM" for _, x in after)
    return r


def evaluate(cfg: dict, runs: int, out: Path, profiles=None, configs=None, echo=print) -> list[dict]:
    from gnss_guardian.detect.ai import AISecondOpinion
    fake = _load_tool("fake_l86")
    out = Path(out)
    (out / "nmea").mkdir(parents=True, exist_ok=True)
    ai_status = AISecondOpinion(cfg, ROOT).status
    ai_label = ai_status.get("label") or f"none ({ai_status['reason']})"
    rows = []
    for name in profiles or PROFILES:
        args, det = PROFILES[name]
        n_runs = 1 if name == "clean" else runs
        for run in range(n_runs):
            seed = 100 + run
            lines = sim_lines(fake, out / "nmea" / f"{name}_{seed}.nmea", args, seed,
                              CLEAN_S if name == "clean" else DURATION)
            for cname in configs or CONFIGS:
                cam, ai, sky = CONFIGS[cname]
                v = run_one(lines, config_for(cfg, cam, ai, sky), cam, fake, seed)
                r = {"profile": name, "run": run, "seed": seed, "config": cname, "camera": cam, "ai": ai, "sky": sky,
                     "expected": det or "-", "minutes": round(len(v) / 60, 1), **score_run(v, det),
                     "source": (f"SIMULATOR fake_l86 seed {seed}, almanac sky {ALMANAC.name}"
                                + ("; camera SIMULATED (true velocity + 0.2 m/s noise)" if cam else "")
                                + (f"; AI model: {ai_label}" if ai else ""))}
                rows.append(r)
                echo(f"{name:20s} run {run} {cname:10s} detected {r['detected']} latency {r['latency_s']} "
                     f"first {r['first_indicator']} FA {r['false_alarm_epochs']}")
    write(rows, out, runs, ai_label)
    return rows


def _lat(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return "-"
    lo, hi, med = min(xs), max(xs), statistics.median(xs)
    return f"{med:g} s" if lo == hi else f"{med:g} s ({lo:g}–{hi:g})"


def summary_md(rows: list[dict], runs: int, ai_label: str) -> str:
    lines = [f"# Evaluation (simulator only, {runs} runs per attack profile, 1 clean run of 30 min)", "",
             "Every number here is from the SIMULATOR (tools/fake_l86.py), not real L86 data. Camera = simulated "
             "velocity (true walk + 0.2 m/s noise). AI model: " + ai_label + ".", "",
             "| Profile | Config | Detected (ALARM) | Latency to ALARM, median (min–max) | to SUSPECT | First indicator "
             "| False-alarm epochs | Wrong detector | Source |",
             "|---|---|---|---|---|---|---|---|---|"]
    keyf = lambda r: (list(PROFILES).index(r["profile"]), list(CONFIGS).index(r["config"]))   # noqa: E731
    groups: dict = {}
    for r in sorted(rows, key=keyf):
        groups.setdefault((r["profile"], r["config"]), []).append(r)
    for (p, c), g in groups.items():
        if p == "clean":
            det = "–"
            fa = f"{sum(r['false_alarm_epochs'] for r in g)} in {sum(r['minutes'] for r in g):g} min"
        else:
            det = f"{sum(bool(r['detected']) for r in g)}/{len(g)}"
            fa = str(sum(r["false_alarm_epochs"] for r in g))
        first = Counter(r["first_indicator"] for r in g if r["first_indicator"]).most_common(1)
        wrong = sum(bool(r["wrong_detector_alarm"]) for r in g)
        lines.append(f"| {p} | {c} | {det} | {_lat([r['latency_s'] for r in g])} | "
                     f"{_lat([r['suspect_latency_s'] for r in g])} | {first[0][0] if first else '-'} | {fa} | "
                     f"{wrong if p != 'clean' else '–'} | simulator, seeds {', '.join(str(r['seed']) for r in g)} |")
    return "\n".join(lines) + "\n"


def write(rows: list[dict], out: Path, runs: int, ai_label: str) -> None:
    with open(out / "results.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    (out / "summary.md").write_text(summary_md(rows, runs, ai_label), encoding="utf-8")
    (out / "eval.json").write_text(json.dumps({
        "format": "gnss-guardian-eval/1", "data": "simulator", "runs": runs, "onset_s": ONSET,
        "written_utc": (datetime.now(timezone.utc) + timedelta(0)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ai_model": ai_label, "rows": rows}, indent=1), encoding="utf-8")
