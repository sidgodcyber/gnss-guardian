"""Score helpers: linear ramps from a config [onset, full] pair, and a 0-1 score with reasons."""
from __future__ import annotations

import math
from dataclasses import dataclass, field


def ramp(x: float | None, span) -> float:
    """0 at span[0], 1 at span[1], linear between, clipped. Works for falling spans
    (span[0] > span[1]: smaller x is worse). None / nan -> 0."""
    if x is None or not math.isfinite(x):
        return 0.0
    lo, hi = float(span[0]), float(span[1])
    if hi == lo:
        return float(x >= hi)
    return min(1.0, max(0.0, (x - lo) / (hi - lo)))


@dataclass
class Score:
    value: float = 0.0
    reasons: list[str] = field(default_factory=list)
    parts: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)     # context that did not raise the score

    def as_dict(self) -> dict:
        return {"score": round(self.value, 3), "reasons": self.reasons,
                "parts": {k: round(v, 3) for k, v in self.parts.items()}, "notes": self.notes}


def weighted_sum(parts: dict[str, float], weights: dict[str, float]) -> float:
    return min(1.0, sum(weights.get(k, 0.0) * v for k, v in parts.items()))


def noisy_or(parts: dict[str, float], weights: dict[str, float]) -> float:
    """1 - prod(1 - w_i s_i): one strong indicator is enough, weak ones accumulate."""
    p = 1.0
    for k, v in parts.items():
        p *= 1.0 - weights.get(k, 0.0) * v
    return 1.0 - p
