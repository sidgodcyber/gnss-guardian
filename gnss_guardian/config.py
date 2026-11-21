"""Configuration: config/default.yaml, optionally overlaid with a user file."""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "default.yaml"


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path: str | Path | None = None, calibration: bool = True) -> dict:
    """Load the default config, then the camera calibration file if it exists (written by
    `gnss-guardian calibrate`), then `path` if given."""
    cfg = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    cal = ROOT / cfg["camera"]["calibration_file"]
    if calibration and cal.exists():
        cfg = _deep_merge(cfg, yaml.safe_load(cal.read_text(encoding="utf-8")) or {})
    if path is not None and Path(path).resolve() != DEFAULT_CONFIG:
        cfg = _deep_merge(cfg, yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})
    return cfg
