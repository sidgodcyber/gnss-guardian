"""AI second opinion (advisory): an anomaly model of clean GNSS epochs.

    model = train(X, cfg, label="...")          # gnss-guardian ai-train: X from collect(...)
    save(model, path)
    ai = AISecondOpinion(cfg, root)               # DetectionEngine: loads ai.model_file
    out = ai.update(features, warming)            # {"score", "level", "reasons", "codes", ...} or None

What it learns: the joint pattern of 11 per-epoch features (C/N0 spread and its drop against the clean
baseline, lost satellites, HDOP against the baseline, newcomer satellites, reported vs implied speed,
altitude rate, GNSS-vs-host clock jumps, C/N0 step from the last epoch, used/tracked) on epochs the rule
detectors call clean. Values are robust-scaled (median, MAD with a per-feature floor) so a clean
recording's quiet features do not make every small wobble look huge.

Two models, one JSON file, both scored with numpy only (no scikit-learn on the Pi at run time):
  iforest       scikit-learn IsolationForest at training time, its trees exported to JSON; score is
                2^(-mean path length / c(max_samples)), exactly sklearn's -score_samples
  mahalanobis   robust centre, shrunk covariance; always fitted
Both get the single-feature outlier check on the training epochs (does one feature full_z robust SD out
score above the training calib_quantile?). The model keeps the ones that pass and runs the chosen one:
Mahalanobis (default) if it passes, else the forest. trained.checks has both results, trained.selection
the data the choice was made on (the model label).
Both are calibrated the same way (ai.train): score 0 at the calib_quantile of the training epochs'
model scores, 1 at the model score of a point that is full_z robust SD from the clean centre in one
feature (median over the features and both signs), so 1 means "as unusual as one feature far out".

How it is combined (ai.*):
  level         CLEAR or SUSPECT only, advisory: SUSPECT when the score is >= suspect_on in on_n of the
                last on_window epochs, CLEAR after clear_n epochs below suspect_off. It never changes
                gnss_trusted, the state machine or the latch; it shows in the verdict, Trust and the UIs.
  corroboration only while the rule spoofing score is >= boost_min_rule:
                spoofing = 1 - (1 - rule)(1 - boost_weight * ai). It can only raise the score: it never
                lowers one, never clears an alarm and never releases the latch.
Reasons are the features furthest from the clean centre (|z| >= explain_z), with codes such as
AI_CN0_STD_LOW. The model's label ("provisional ...", "real L86 ...") travels with every output.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

FORMAT = "gnss-guardian-ai/1"
FEATURES = ("cn0_std", "mean_drop_db", "tracked_drop_db", "lost_frac", "hdop_ratio", "newcomer_frac",
            "speed_mismatch_mps", "alt_rate_mps", "time_jump_s", "cn0_step_db", "used_frac")
DESCR = {
    "cn0_std": "C/N0 spread across satellites",
    "mean_drop_db": "C/N0 drop against the clean baseline",
    "tracked_drop_db": "C/N0 drop of the satellites still tracked",
    "lost_frac": "share of baseline satellites lost",
    "hdop_ratio": "HDOP against the baseline",
    "newcomer_frac": "share of satellites the baseline never saw",
    "speed_mismatch_mps": "reported speed vs speed implied by the fixes",
    "alt_rate_mps": "altitude rate",
    "time_jump_s": "GNSS clock against the host clock",
    "cn0_step_db": "C/N0 step from the last epoch",
    "used_frac": "share of tracked satellites used in the fix",
}
EULER = 0.5772156649015329


class AIFeatures:
    """The model's feature vector from one epoch's rule features (keeps the last C/N0 mean)."""

    def __init__(self):
        self._prev_cn0: float | None = None

    def vector(self, f: dict) -> list[float]:
        def g(k):
            x = f.get(k)
            return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else math.nan
        imp, sog = g("implied_speed_mps"), g("sog_mps")
        cn0 = g("cn0_mean")
        step = abs(cn0 - self._prev_cn0) if self._prev_cn0 is not None and math.isfinite(cn0) else math.nan
        self._prev_cn0 = cn0 if math.isfinite(cn0) else None
        nt, nu = g("n_tracked"), g("n_used")
        vals = {"cn0_std": g("cn0_std"), "mean_drop_db": g("mean_drop_db"), "tracked_drop_db": g("tracked_drop_db"),
                "lost_frac": g("lost_frac"), "hdop_ratio": g("hdop_ratio"), "newcomer_frac": g("newcomer_frac"),
                "speed_mismatch_mps": abs(imp - sog) if math.isfinite(imp) and math.isfinite(sog) else math.nan,
                "alt_rate_mps": g("alt_rate_mps"), "time_jump_s": g("time_jump_s"), "cn0_step_db": step,
                "used_frac": nu / nt if nt and math.isfinite(nu) and nt > 0 else math.nan}
        return [vals[k] for k in FEATURES]


# ---------------------------------------------------------------- training (laptop or Pi)
def collect(lines_list, cfg: dict, first_s: float | None = None, skip_s: float | None = None) -> np.ndarray:
    """Feature vectors of the clean, warmed-up epochs with a valid fix, from NMEA files' lines, using the
    rule engine (AI off). ``skip_s`` / ``first_s``: only epochs in [skip_s, first_s) s from each file's start."""
    import copy

    from gnss_guardian.detect.engine import DetectionEngine
    from gnss_guardian.io.epochs import epochs_from_lines
    c = copy.deepcopy(cfg)
    c["ai"]["enabled"] = False
    rows = []
    for lines in lines_list:
        eng, feats, t0 = DetectionEngine(c), AIFeatures(), None
        for ep in epochs_from_lines(lines, c):
            v = eng.update(ep)
            vec = feats.vector(v["features"])
            t = elapsed(ep)
            t0 = t if t0 is None else t0
            dt = t - t0
            if (skip_s is not None and dt < skip_s) or (first_s is not None and dt >= first_s):
                continue
            if not v["warming_up"] and v["clean"] and v["fix_valid"]:
                rows.append(vec)
    return np.array(rows, dtype=float).reshape(-1, len(FEATURES))


def elapsed(ep) -> float:
    if ep.mono is not None:
        return ep.mono
    if ep.t_gnss is not None:
        return ep.t_gnss.timestamp()
    return float(ep.seq)


def _c(n: float) -> float:
    """Average path length of an unsuccessful BST search (sklearn _average_path_length)."""
    if n <= 1:
        return 0.0
    if n <= 2:
        return 1.0
    return 2.0 * (math.log(n - 1.0) + EULER) - 2.0 * (n - 1.0) / n


def _calib(scores: np.ndarray, ref: np.ndarray, q: float) -> dict:
    """0 at quantile q of the training scores, 1 at the median score of the reference outliers."""
    med, hi = float(np.median(scores)), float(np.quantile(scores, q))
    full = float(np.median(ref))
    if not full > hi:
        raise ValueError("the model does not separate full_z outliers from its training epochs")
    return {"median": med, "q": hi, "full": full, "quantile": q}


def _outliers(mu: np.ndarray, full_z: float) -> np.ndarray:
    """One feature full_z robust SD from the clean centre (both signs), the others at the centre."""
    k = len(mu)
    return np.vstack([np.eye(k) * full_z, -np.eye(k) * full_z]) + mu


def _check(scores: np.ndarray, ref: np.ndarray, q: float, full_z: float) -> dict:
    """Single-feature outlier check: does one feature full_z robust SD out score above the training quantile?"""
    out = {"train_q": round(float(np.quantile(scores, q)), 4), "outlier_median": round(float(np.median(ref)), 4)}
    try:
        out["calib"] = _calib(scores, ref, q)
        out["passed"] = True
    except ValueError:
        out["passed"] = False
    out["detail"] = (f"one feature {full_z:g} robust SD out scores {out['outlier_median']:.3f} vs the training "
                     f"{q} quantile {out['train_q']:.3f}: {'passed' if out['passed'] else 'failed'}")
    return out


def train(X: np.ndarray, cfg: dict, label: str, sources: list[str] | None = None, backend: str = "auto") -> dict:
    """Both models are fitted (the forest when scikit-learn is installed) and both get the single-feature
    outlier check on these training epochs. Kept: the ones that pass. Chosen: Mahalanobis (the default)
    if it passes, else the forest; ``backend`` forces one (an error if it fails). ``trained.selection``
    says on which data (``label``) the choice was made."""
    t = cfg["ai"]["train"]
    X = np.asarray(X, dtype=float)
    if len(X) < t["min_epochs"]:
        raise ValueError(f"only {len(X)} clean epochs to train on; need at least {t['min_epochs']}")
    center = np.nanmedian(X, axis=0)
    center = np.where(np.isfinite(center), center, 0.0)
    mad = np.nanmedian(np.abs(X - center), axis=0) * 1.4826
    floor = np.array([t["scale_floor"][k] for k in FEATURES], dtype=float)
    scale = np.maximum(np.where(np.isfinite(mad), mad, 0.0), floor)
    Z = _z(X, center, scale)
    R = _outliers(np.median(Z, axis=0), t["full_z"])
    q = t["calib_quantile"]
    cov = np.cov(Z.T)
    a = t["shrink"]
    cov = (1 - a) * cov + a * np.eye(len(FEATURES)) * max(float(np.trace(cov)) / len(FEATURES), 1e-6)
    inv = np.linalg.inv(cov)
    mu = Z.mean(axis=0)
    maha = lambda M: np.sqrt(np.einsum("ij,jk,ik->i", M - mu, inv, M - mu))   # noqa: E731
    checks = {"mahalanobis": _check(maha(Z), maha(R), q, t["full_z"])}
    fits = {"mahalanobis": {"mean": mu.round(6).tolist(), "inv_cov": inv.round(8).tolist()}}
    sk = None
    if backend in ("auto", "iforest"):
        try:
            import sklearn
            from sklearn.ensemble import IsolationForest
            sk = sklearn.__version__
        except ImportError:
            if backend == "iforest":
                raise
        if sk:
            f = IsolationForest(n_estimators=t["n_estimators"], max_samples=min(t["max_samples"], len(Z)),
                                random_state=t["seed"]).fit(Z)
            fits["iforest"] = export_forest(f)
            checks["iforest"] = _check(_iforest_scores(fits["iforest"], Z), _iforest_scores(fits["iforest"], R),
                                       q, t["full_z"])
    passed = [k for k in ("mahalanobis", "iforest") if k in checks and checks[k]["passed"]]
    if backend != "auto":
        if backend not in passed:
            raise ValueError(f"{backend} failed the outlier check: {checks[backend]['detail']}")
        chosen = backend
    elif passed:
        chosen = passed[0]
    else:
        raise ValueError("neither model separates a single-feature outlier from the training epochs: "
                         + "; ".join(f"{k} {c['detail']}" for k, c in checks.items()))
    model = {"format": FORMAT, "label": label, "features": list(FEATURES), "chosen": chosen,
             "center": center.round(6).tolist(), "scale": scale.round(6).tolist(),
             "mahalanobis": None, "iforest": None,
             "trained": {"utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "epochs": len(X),
                         "sources": sources or [], "sklearn": sk,
                         "checks": {k: {kk: v for kk, v in c.items() if kk != "calib"} for k, c in checks.items()},
                         "selection": f"{chosen}, chosen by the single-feature outlier check on: {label}"}}
    for k in passed:
        model[k] = {**fits[k], "calib": checks[k]["calib"]}
    return model


def export_forest(f) -> dict:
    """A fitted sklearn IsolationForest as plain lists (scored by _iforest_scores, numpy only)."""
    trees = []
    for est, feats in zip(f.estimators_, f.estimators_features_):
        tr = est.tree_
        trees.append({"left": tr.children_left.tolist(), "right": tr.children_right.tolist(),
                      "feature": [int(feats[i]) if i >= 0 else -1 for i in tr.feature],
                      "threshold": [float(x) for x in tr.threshold], "n": tr.n_node_samples.tolist()})
    return {"max_samples": int(f.max_samples_), "trees": trees}


def _z(X: np.ndarray, center, scale) -> np.ndarray:
    X = np.where(np.isfinite(X), X, center)                  # missing: the clean centre (neutral)
    return np.clip((X - center) / scale, -50, 50)


def _iforest_scores(m: dict, Z: np.ndarray) -> np.ndarray:
    """2^(-E[h(x)] / c(max_samples)) for each row: sklearn's -score_samples (higher = more anomalous)."""
    Z = np.atleast_2d(Z)
    total = np.zeros(len(Z))
    for tr in m["trees"]:
        left, right, feat, thr, n = tr["left"], tr["right"], tr["feature"], tr["threshold"], tr["n"]
        for r, z in enumerate(Z):
            node, depth = 0, 0
            while left[node] != -1:
                node = left[node] if z[feat[node]] <= thr[node] else right[node]
                depth += 1
            total[r] += depth + _c(n[node])
    return 2.0 ** (-(total / len(m["trees"])) / _c(m["max_samples"]))


def save(model: dict, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(model, separators=(",", ":")), encoding="utf-8")


# ---------------------------------------------------------------- run time
class AIModel:
    def __init__(self, model: dict, backend: str = "auto"):
        if model.get("format") != FORMAT or model.get("features") != list(FEATURES):
            raise ValueError(f"not a {FORMAT} model with features {', '.join(FEATURES)}")
        self.m = model
        self.label = model["label"]
        self.center, self.scale = np.array(model["center"]), np.array(model["scale"])
        if backend in ("iforest", "mahalanobis") and not model.get(backend):
            raise ValueError(f"the model has no {backend} part (it failed the outlier check or was not trained)")
        self.backend = model.get("chosen", "mahalanobis") if backend == "auto" else backend
        if self.backend == "mahalanobis":
            mh = model["mahalanobis"]
            self._mu, self._inv = np.array(mh["mean"]), np.array(mh["inv_cov"])
        self.calib = model[self.backend]["calib"]

    def raw(self, z: np.ndarray) -> float:
        if self.backend == "iforest":
            return float(_iforest_scores(self.m["iforest"], z)[0])
        d = z - self._mu
        return float(math.sqrt(max(0.0, d @ self._inv @ d)))

    def score(self, vec) -> tuple[float, float, np.ndarray]:
        """(0-1 score, raw model score, z vector) of one feature vector."""
        z = _z(np.array(vec, dtype=float)[None, :], self.center, self.scale)[0]
        r = self.raw(z)
        c = self.calib
        return min(1.0, max(0.0, (r - c["q"]) / (c["full"] - c["q"]))), r, z


class AISecondOpinion:
    """Per engine: loads the model once, scores every warmed-up epoch, keeps the advisory level."""

    def __init__(self, cfg: dict, root: Path | None = None):
        self.c = c = cfg["ai"]
        self.model: AIModel | None = None
        self.feats = AIFeatures()
        self.level = "CLEAR"
        self._hist: list[bool] = []
        self._below = 0
        path = c.get("model_file")
        if not c.get("enabled", True):
            self.status = {"enabled": False, "reason": "AI second opinion disabled in config (ai.enabled)"}
            return
        if not path:
            self.status = {"enabled": False, "reason": "no AI model configured (ai.model_file)"}
            return
        p = Path(path)
        if not p.is_absolute() and root is not None:
            p = root / p
        if not p.is_file():
            self.status = {"enabled": False, "reason": f"AI model {path} not found: gnss-guardian ai-train"}
            return
        try:
            self.model = AIModel(json.loads(p.read_text(encoding="utf-8")), c.get("backend", "auto"))
        except (OSError, ValueError, KeyError) as e:
            self.status = {"enabled": False, "reason": f"AI model {path} unusable: {e}"}
            return
        tr = self.model.m["trained"]
        self.status = {"enabled": True, "backend": self.model.backend, "label": self.model.label,
                       "reason": f"AI model {path}: {self.model.backend}, {tr['epochs']} training epochs, "
                                 f"{self.model.label}"}

    @property
    def enabled(self) -> bool:
        return self.model is not None

    def update(self, f: dict, warming: bool) -> dict | None:
        if self.model is None:
            return None
        vec = self.feats.vector(f)
        out = {"score": 0.0, "raw": None, "level": "CLEAR", "reasons": [], "codes": [],
               "backend": self.model.backend, "model": self.model.label}
        if warming:
            self._hist, self._below, self.level = [], 0, "CLEAR"
            return out
        s, r, z = self.model.score(vec)
        c = self.c
        self._hist = (self._hist + [s >= c["suspect_on"]])[-c["on_window"]:]
        self._below = self._below + 1 if s < c["suspect_off"] else 0
        if self.level == "CLEAR" and sum(self._hist) >= c["on_n"]:
            self.level = "SUSPECT"
        elif self.level == "SUSPECT" and self._below >= c["clear_n"]:
            self.level = "CLEAR"
        out.update(score=round(s, 3), raw=round(r, 4), level=self.level)
        if s > 0:
            for i in np.argsort(-np.abs(z))[:2]:
                if abs(z[i]) < c["explain_z"]:
                    break
                k, hi = FEATURES[i], z[i] > 0
                out["codes"].append(f"AI_{k.upper().removesuffix('_MPS').removesuffix('_DB').removesuffix('_S')}_"
                                    f"{'HIGH' if hi else 'LOW'}")
                out["reasons"].append(f"AI: {DESCR[k]} unusually {'high' if hi else 'low'} "
                                      f"({z[i]:+.1f} robust SD from clean)")
        return out


def corroborate(rule: float, ai: dict | None, cfg: dict) -> float:
    """The spoofing score with the AI's corroboration: never below ``rule``."""
    c = cfg["ai"]
    if ai is None or rule < c["boost_min_rule"] or not ai["score"]:
        return rule
    return max(rule, 1.0 - (1.0 - rule) * (1.0 - c["boost_weight"] * ai["score"]))


def evaluate(lines, cfg: dict, skip_s: float | None = None, first_s: float | None = None) -> dict:
    """AI on one NMEA file (model from cfg["ai"]): epochs judged, AI SUSPECT epochs and episodes, score
    quantiles, and spoofing ALARM epochs with and without the corroboration, in [skip_s, first_s)."""
    import copy

    from gnss_guardian.detect.engine import DetectionEngine
    from gnss_guardian.io.epochs import epochs_from_lines
    off = copy.deepcopy(cfg)
    off["ai"]["enabled"] = False
    eng, ref = DetectionEngine(cfg), DetectionEngine(off)
    if eng.ai is None or not eng.ai.enabled:
        raise ValueError(eng.ai.status["reason"] if eng.ai else "no ai section in the config")
    n = sus = episodes = alarm = alarm_ref = 0
    scores, t0, prev = [], None, "CLEAR"
    for ep in epochs_from_lines(lines, cfg):
        v, r = eng.update(ep), ref.update(ep)
        t = elapsed(ep)
        t0 = t if t0 is None else t0
        if (skip_s is not None and t - t0 < skip_s) or (first_s is not None and t - t0 >= first_s) or v["warming_up"]:
            continue
        n += 1
        a = v["ai"]
        scores.append(a["score"])
        sus += a["level"] == "SUSPECT"
        episodes += a["level"] == "SUSPECT" and prev == "CLEAR"
        prev = a["level"]
        alarm += v["spoofing"]["level"] == "ALARM"
        alarm_ref += r["spoofing"]["level"] == "ALARM"
    sc = np.array(scores) if scores else np.zeros(1)
    return {"epochs": n, "ai_suspect_epochs": sus, "ai_suspect_episodes": episodes,
            "score_median": round(float(np.median(sc)), 3), "score_p99": round(float(np.quantile(sc, 0.99)), 3),
            "score_max": round(float(sc.max()), 3), "spoof_alarm_epochs": alarm, "spoof_alarm_epochs_rule_only": alarm_ref,
            "backend": eng.ai.model.backend, "model": eng.ai.model.label}
