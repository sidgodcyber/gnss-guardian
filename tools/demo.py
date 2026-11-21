"""One-command demo: render a synthetic walk, simulate a jam and a spoof on it, replay both through the
whole pipeline (detection, vision, state machine, guidance) and start the dashboard on them.

    python tools/demo.py [--sessions session] [--work demo] [--fresh] [--no-dashboard] [--port 3000] [--open]

The walk: stand 30 s (warm-up and home), 12 m east and back at 1.2 m/s, stand at home. The camera
video is rendered ground (tools/synth_walk.py), the NMEA comes from tools/fake_l86.py. Both are
synthetic: the demo shows the logic, not field performance.

  demo_jam    sudden jam near the far end (fix lost), recovers 30 s later:
              JAMMED -> VISION_NAV -> RETURN_HOME -> HOME_REACHED -> GNSS_OK
  demo_spoof  500 m position jump with natural C/N0 near the far end, ends 42 s later (after the
              detector's own latch has expired): SPOOFED -> VISION_NAV -> RETURN_HOME -> HOME_REACHED,
              GNSS stays untrusted until it agrees with the camera for state.spoof_release.agree_s

The demo shortens the warm-up (20 s) and the home window (5 s) through a config overlay written to
WORK/demo.yaml, so the sessions stay short; every other threshold is config/default.yaml. The rendered
walk and the NMEA go to WORK (default demo/, git-ignored), the sessions to SESSIONS/demo_jam and
SESSIONS/demo_spoof. The dashboard is `npm run dev` in dashboard/ on 127.0.0.1:PORT (it fails if the port is
taken, rather than moving to another one); Ctrl-C stops it.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
START = "2026-10-22T14:00:00Z"
WALK, STILL_S, FPS = "outback:12:90:1.2", 30, 15
ONSET = STILL_S + 9                                   # near the far end of the 12 m leg
SCENARIOS = {
    "demo_jam": (85, ["--jam", "sudden", "--jam-at", str(ONSET), "--recover-after", "30"]),
    "demo_spoof": (100, ["--spoof", "jump", "--spoof-cn0", "natural", "--spoof-at", str(ONSET),
                         "--recover-after", "42"]),
}
OVERLAY = "detect:\n  baseline:\n    warmup_s: 20\nhome:\n  set_after_s: 5\n"


def tool(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def build(work: Path, sessions: Path, fresh: bool, echo=print) -> dict:
    """Render (once), simulate and replay both scenarios; returns {session name: state sequence}."""
    sys.path.insert(0, str(ROOT))
    from gnss_guardian.config import load_config
    from gnss_guardian.run import replay

    work.mkdir(parents=True, exist_ok=True)
    walk = work / "walk"
    duration = max(d for d, _ in SCENARIOS.values())
    if fresh or not (walk / "video.avi").exists():
        echo(f"rendering the walk ({duration} s at {FPS} fps) into {walk} ...")
        tool("synth_walk").main([str(walk), "--walk", WALK, "--still-s", str(STILL_S), "--duration-s", str(duration),
                                 "--fps", str(FPS)])
    (work / "demo.yaml").write_text(OVERLAY, encoding="utf-8")
    cfg = load_config(work / "demo.yaml")
    fk = tool("fake_l86")
    t0 = datetime.fromisoformat(START.replace("Z", "+00:00"))
    out = {}
    for name, (dur, profile) in SCENARIOS.items():
        nmea = work / f"{name}.nmea"
        a = fk.build_parser().parse_args(["--write", str(nmea), "--start", START, "--duration-s", str(dur),
                                          "--walk", WALK, "--still-s", str(STILL_S), *profile])
        fk.write_file(a, fk.load_cycles(fk.FIXTURE), t0)
        echo(f"replaying {name} ({' '.join(profile)}) ...")
        s = replay(nmea, cfg, sessions / name, video=walk / "video.avi",
                   echo=lambda line: echo(f"  {line}") if "state " in line else None)
        out[name] = s["states"]
        echo(f"  {name}: {' -> '.join(s['states'])} | dashboard {s['dashboard']}")
    return out


def dashboard(sessions: Path, open_browser: bool, port: int = 3000) -> int:
    npm = shutil.which("npm")
    if npm is None:
        print("npm not found: install Node.js, or open the session folders with the built dashboard (dashboard/README.md)")
        return 1
    dash = ROOT / "dashboard"
    if not (dash / "node_modules").exists():
        print("installing dashboard dependencies (npm ci) ...")
        subprocess.run([npm, "ci"], cwd=dash, check=True)
    url = f"http://127.0.0.1:{port}/?session=demo_jam"
    print(f"dashboard: {url}  (Ctrl-C stops it)")
    if open_browser:
        webbrowser.open(url)
    try:
        return subprocess.run([npm, "run", "dev", "--", "--port", str(port), "--strictPort"], cwd=dash,
                              env={**os.environ, "GG_SESSIONS": str(sessions)}).returncode
    except KeyboardInterrupt:
        return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sessions", default=str(ROOT / "session"), help="session folders for the dashboard")
    ap.add_argument("--work", default=str(ROOT / "demo"), help="rendered walk, NMEA and the config overlay")
    ap.add_argument("--fresh", action="store_true", help="render the walk again even if it exists")
    ap.add_argument("--no-dashboard", action="store_true", help="build the sessions only")
    ap.add_argument("--port", type=int, default=3000, help="dashboard port (must be free)")
    ap.add_argument("--open", action="store_true", help="open the dashboard in the browser")
    a = ap.parse_args(argv)
    sessions = Path(a.sessions).resolve()
    build(Path(a.work).resolve(), sessions, a.fresh)
    return 0 if a.no_dashboard else dashboard(sessions, a.open, a.port)


if __name__ == "__main__":
    sys.exit(main())
