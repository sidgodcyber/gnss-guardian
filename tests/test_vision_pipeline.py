"""Vision wired into the whole stack: calibration, camera sources, replay of NMEA + video through
detection (observe_vision), live mode with a camera thread, the Pi recorder, the frame-rate bench."""
import copy
import importlib.util
import json
import math
import subprocess
import sys
from datetime import datetime, timezone

import pytest
import yaml
from conftest import ROOT, START, SYNTH_STILL_S, fake_l86, fast_vision_cfg, synth_walk

from gnss_guardian.io.serial_live import run_live
from gnss_guardian.run import replay
from gnss_guardian.session import read_jsonl
from gnss_guardian.vision import bench
from gnss_guardian.vision.calibrate import calibrate, write_calibration
from gnss_guardian.vision.camera import CameraUnavailable, open_camera


def test_calibrate_fits_focal_from_a_measured_walk(cfg, tmp_path):
    traj = synth_walk.Trajectory(straight="10:1.2:90", still_s=1.0)
    synth_walk.write_session(tmp_path / "walk", traj, synth_walk.Renderer(), 10 / 1.2 + 2.5, 30,
                             datetime(2026, 10, 22, 14, tzinfo=timezone.utc))
    wrong = copy.deepcopy(cfg)
    wrong["camera"]["focal_px"] = 700.0                                   # 10 % off the true 635 px
    r = calibrate(tmp_path / "walk" / "video.avi", 10.0, wrong)
    print(f"calibrate: {r}")
    assert abs(r["focal_px"] - 635.0) / 635.0 < 0.015 and r["straightness"] > 0.95
    path = write_calibration(r, cfg, tmp_path / "calibration.yaml")
    assert yaml.safe_load(path.read_text())["camera"]["focal_px"] == r["focal_px"]


def test_camera_sources(cfg, synth_session):
    cam = open_camera(f"video:{synth_session / 'video.avi'}", cfg)
    f0, f1 = cam.read(), cam.read()
    cam.close()
    t0 = datetime.fromisoformat(START.replace("Z", "+00:00")).timestamp()
    assert f0.gray.shape == (480, 640) and abs(f0.t - t0) < 1e-3 and abs(f1.t - f0.t - 1 / 15) < 2e-3
    if importlib.util.find_spec("picamera2") is None:                    # laptop: guarded, clear message
        with pytest.raises(CameraUnavailable, match="Raspberry Pi OS"):
            open_camera("picam", cfg)


def _vision_rows(path):
    return [v for v in read_jsonl(path) if v.get("vision") and v["vision"]["e"] is not None and v["enu_m"]]


def test_replay_with_video_clean(cfg, synth_session, tmp_path):
    c = fast_vision_cfg(cfg)
    s = replay(synth_session / "nmea.nmea", c, tmp_path / "s", video=synth_session / "video.avi")
    assert s["spoofing_suspect_epochs"] == 0 and s["spoofing_alarm_epochs"] == 0 and s["jamming_alarm_epochs"] == 0
    assert s["frames"] > 1000 and s["keyframes"] >= 10
    rows = _vision_rows(tmp_path / "s" / "epochs.jsonl")
    d = sorted(math.hypot(v["vision"]["e"] - v["enu_m"][0], v["vision"]["n"] - v["enu_m"][1]) for v in rows)
    print(f"vision vs GNSS while trusted: median {d[len(d) // 2]:.2f} m, max {d[-1]:.2f} m over {len(d)} epochs")
    assert d[len(d) // 2] < 0.3 and d[-1] < 1.0
    assert any(v["features"]["vision_diff_mps"] is not None for v in rows)    # observe_vision is wired
    ev = read_jsonl(tmp_path / "s" / "events.jsonl")
    assert any(e["kind"] == "keyframe" and e["home"] for e in ev)
    assert list((tmp_path / "s" / "keyframes").glob("kf_*_home.jpg"))


@pytest.mark.parametrize("drift,within", [(1.0, 4), (0.5, 10)])
def test_natural_drift_caught_by_vision(cfg, synth_session, tmp_path, drift, within):
    """Spoofed drag-off with the real satellites and C/N0 (invisible to GNSS-only checks): the
    camera's velocity disagrees and the spoofing alarm follows."""
    onset = SYNTH_STILL_S + 20
    a = fake_l86.build_parser().parse_args([
        "--write", str(tmp_path / "drift.nmea"), "--start", START, "--duration-s", "75", "--walk", "loop:20:10:1.2",
        "--still-s", str(SYNTH_STILL_S), "--spoof", "drift", "--spoof-cn0", "natural", "--spoof-at", str(onset),
        "--drift-mps", str(drift)])
    fake_l86.write_file(a, fake_l86.load_cycles(fake_l86.FIXTURE), datetime.fromisoformat(START.replace("Z", "+00:00")))
    s = replay(tmp_path / "drift.nmea", fast_vision_cfg(cfg), tmp_path / "s", video=synth_session / "video.avi")
    eps = read_jsonl(tmp_path / "s" / "epochs.jsonl")
    alarm = next((v for v in eps if v["spoofing"]["level"] == "ALARM"), None)
    assert alarm is not None and 0 <= alarm["seq"] - onset <= within, alarm and alarm["seq"]
    assert all(v["spoofing"]["level"] == "CLEAR" for v in eps[:onset])
    assert any("vision" in r for r in alarm["spoofing"]["reasons"])
    assert s["jamming_alarm_epochs"] == 0


def test_live_with_camera_thread_and_without_camera(cfg, synth_session, tmp_path):
    c = copy.deepcopy(cfg)
    c["live"]["reconnect_s"] = 0.1
    p = subprocess.Popen([sys.executable, str(ROOT / "tools" / "fake_l86.py"), "--transport", "tcp", "--start", START,
                          "--duration-s", "20"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    port = p.stdout.readline().split()[1]
    try:
        run_live(port, 9600, tmp_path / "cam", c, duration_s=6, camera=f"video:{synth_session / 'video.avi'}")
    finally:
        p.kill()
        p.wait(timeout=10)
    ev = {e["kind"]: e for e in read_jsonl(tmp_path / "cam" / "events.jsonl")}
    assert "camera_open" in ev and ev["camera_stopped"]["frames"] >= 60 and ev["camera_stopped"]["error"] is None
    assert all("vision" in v for v in read_jsonl(tmp_path / "cam" / "epochs.jsonl"))
    if importlib.util.find_spec("picamera2") is None:                    # no camera: GNSS-only, and it says so
        p = subprocess.Popen([sys.executable, str(ROOT / "tools" / "fake_l86.py"), "--transport", "tcp", "--start",
                              START, "--duration-s", "10"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        port = p.stdout.readline().split()[1]
        try:
            s = run_live(port, 9600, tmp_path / "nocam", c, duration_s=3, camera="picam")
        finally:
            p.kill()
            p.wait(timeout=10)
        kinds = [e["kind"] for e in read_jsonl(tmp_path / "nocam" / "events.jsonl")]
        assert "camera_unavailable" in kinds and s["epochs"] >= 2


def test_record_session_tool(synth_session, tmp_path):
    spec = importlib.util.spec_from_file_location("record_session", ROOT / "tools" / "record_session.py")
    rec = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rec)
    out = tmp_path / "rec"
    assert rec.main(["--out", str(out), "--camera", f"video:{synth_session / 'video.avi'}", "--no-nmea",
                     "--duration-min", "0.04"]) == 0
    rows = (out / "frames.csv").read_text().splitlines()
    assert rows[0] == "idx,host_utc" and len(rows) > 20
    cam = open_camera(f"video:{out / 'video.avi'}", {"camera": {"width": 640, "height": 480, "fps": 15}})
    n = 0
    while cam.read() is not None:
        n += 1
    assert n == len(rows) - 1


def test_vision_bench(cfg):
    r = bench.run(cfg, n=40)
    assert r["odometry_fps"] > 0 and r["orb_ms"] > 0 and r["frames"] == 40
    print(json.dumps(r))
