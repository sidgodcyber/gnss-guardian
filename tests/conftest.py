import copy
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gnss_guardian.config import load_config
from gnss_guardian.detect.engine import DetectionEngine
from gnss_guardian.io.epochs import epochs_from_lines

ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).parent / "fixtures"
SIM_FIXTURE = FIX / "l86_sim_10min.nmea"
START = "2026-10-22T14:00:00Z"
T0 = datetime(2026, 10, 22, 14, 0, 0, tzinfo=timezone.utc)


def _load_tool(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


fake_l86 = _load_tool("fake_l86")


@pytest.fixture(scope="session")
def cfg():
    return load_config()


@pytest.fixture
def cfg_copy(cfg):
    return copy.deepcopy(cfg)


def nmea(body: str) -> str:
    x = 0
    for ch in body:
        x ^= ord(ch)
    return f"${body}*{x:02X}"


def sim_file(path: Path, *args, duration_s=300, walk="loop:20:10:1.2") -> Path:
    """Render a simulator run to a host-prefixed NMEA file (fake_l86 --write, in-process)."""
    argv = ["--write", str(path), "--start", START, "--duration-s", str(duration_s), *args]
    if walk:
        argv += ["--walk", walk]
    a = fake_l86.build_parser().parse_args(argv)
    fake_l86.write_file(a, fake_l86.load_cycles(SIM_FIXTURE), T0)
    return path


def run_engine(lines, cfg, vision=None) -> list[dict]:
    """Verdict per epoch; ``vision(epoch) -> (v_e, v_n, quality) | None`` feeds the Phase 2 hook."""
    eng = DetectionEngine(cfg)
    out = []
    for ep in epochs_from_lines(lines, cfg):
        if vision is not None and ep.mono is not None:
            vv = vision(ep)
            if vv is not None:
                eng.observe_vision(ep.mono, *vv)
        out.append(eng.update(ep))
    return out


def first_level(verdicts, detector, level="ALARM", rate_hz=1.0):
    """Host-side time (s from T0, from the epoch sequence at ``rate_hz``; GNSS time may be the thing
    being spoofed) of the first epoch at ``level`` for ``detector``, or None."""
    for v in verdicts:
        if v[detector]["level"] == level:
            return v["seq"] / rate_hz
    return None


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


synth_walk = _load_tool("synth_walk")
SYNTH_STILL_S, SYNTH_DURATION_S, SYNTH_FPS = 30, 75, 15        # home is set at ~27 s: still standing


def fast_vision_cfg(cfg):
    """Shorter warm-up and home window so synthetic sessions stay short (vision behaviour unchanged)."""
    c = copy.deepcopy(cfg)
    c["detect"]["baseline"]["warmup_s"] = 20
    c["home"]["set_after_s"] = 5
    return c


@pytest.fixture(scope="session")
def synth_session(tmp_path_factory):
    """One rendered walk shared by the vision pipeline tests: stand 30 s, then the 20 x 10 m loop at
    1.2 m/s; video.avi + frames.csv + truth.csv + nmea.nmea on one clock."""
    out = tmp_path_factory.mktemp("synth")
    synth_walk.main([str(out), "--walk", "loop:20:10:1.2", "--still-s", str(SYNTH_STILL_S),
                     "--duration-s", str(SYNTH_DURATION_S), "--fps", str(SYNTH_FPS), "--nmea"])
    return out
