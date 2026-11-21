"""Road-demo setup: the camera exposure cap, calibration from a short push at the rig's own height
(from a video and live from the camera), and the one-command field demo."""
import copy
from datetime import datetime, timezone
from unittest import mock

import yaml
from click.testing import CliRunner
from conftest import synth_walk

from gnss_guardian.cli import cli
from gnss_guardian.vision.calibrate import calibrate_frames, write_calibration
from gnss_guardian.vision.camera import ExposureCap, Frame


def test_exposure_cap_keeps_the_shutter_short():
    cap = ExposureCap(3000, max_gain=8.0, target=110, every=1)
    assert cap.step(2000, 1.0, 110) is None                       # auto exposure under the cap: leave it
    ctl = cap.step(12000, 2.0, 110)                                # dusk: AE wants 12 ms
    assert ctl == {"AeEnable": False, "ExposureTime": 3000, "AnalogueGain": 8.0} and cap.manual
    assert cap.step(3000, 8.0, 108) is None                        # close to target: no change
    ctl = cap.step(3000, 8.0, 220)                                 # brighter: gain down
    assert ctl and ctl["AnalogueGain"] < 8.0
    for _ in range(20):
        ctl = cap.step(3000, cap.gain, 250) or ctl                 # full sun: gain reaches 1, then AE again
    assert ctl == {"AeEnable": True} and not cap.manual


def walk_frames(dist_m, height_m, fps=30):
    traj = synth_walk.Trajectory(straight=f"{dist_m}:1.2:0", still_s=1.0, height_m=height_m)
    t0 = datetime(2026, 10, 22, 14, tzinfo=timezone.utc).timestamp()
    for k, (t, p, img) in enumerate(synth_walk.frames(traj, synth_walk.Renderer(), dist_m / 1.2 + 2.5, fps)):
        yield Frame(k, t0 + t, img)


def test_calibrate_from_a_5_m_push_at_rig_height(cfg, tmp_path):
    wrong = copy.deepcopy(cfg)
    wrong["camera"]["focal_px"] = 580.0                            # 9 % off the true 635 px
    r = calibrate_frames(walk_frames(5.0, 0.8), 5.0, wrong, height_m=0.8, source="synthetic 5 m push")
    print(f"5 m push at 0.8 m: {r}")
    assert abs(r["focal_px"] - 635.0) / 635.0 < 0.02 and r["height_m"] == 0.8 and r["straightness"] > 0.95
    c = copy.deepcopy(cfg)
    c["camera"]["calibration_file"] = str(tmp_path / "calibration.yaml")    # the file load_config merges on the Pi
    path = write_calibration(r, c)
    assert path == tmp_path / "calibration.yaml"
    assert yaml.safe_load(path.read_text())["camera"] == {"focal_px": r["focal_px"], "height_m": 0.8}


def test_calibrate_live_from_the_camera(cfg, tmp_path):
    class FakeCam:                                                 # the Pi camera, pushed 5 m along the road
        def __init__(self):
            self.it = walk_frames(5.0, 1.0)

        def read(self):
            return next(self.it, None)

        def close(self):
            pass
    with mock.patch("gnss_guardian.vision.camera.open_camera", return_value=FakeCam()), \
            mock.patch("gnss_guardian.vision.calibrate.write_calibration", return_value=tmp_path / "c.yaml") as w:
        r = CliRunner().invoke(cli, ["calibrate", "--live", "--distance", "5"], input="\n")
    assert r.exit_code == 0, r.output
    assert "Push the rig straight 5 m" in r.output and f"wrote {tmp_path / 'c.yaml'}" in r.output
    fit = w.call_args.args[0]
    assert abs(fit["true_m"] - 5.0) < 1e-9 and fit["frames"] > 100
    assert CliRunner().invoke(cli, ["calibrate", "--distance", "5"]).exit_code == 2         # neither source


def test_field_demo_starts_live_with_everything_on(cfg):
    with mock.patch("gnss_guardian.io.serial_live.run_live",
                    return_value={"epochs": 3, "states": ["WARMING_UP"]}) as m:
        r = CliRunner().invoke(cli, ["field-demo", "--out", "session/road1", "--duration-s", "1"])
    assert r.exit_code == 0, r.output
    args, kw = m.call_args
    assert args[0] == "/dev/serial0" and args[2] == "session/road1"
    assert kw["overlay_port"] == 8080 and kw["demo_inject"] and kw["overlay_record"]
    assert kw["camera"] == cfg["camera"]["source"] and "DEMO ONLY" in r.output
    assert cfg["vision"]["process_scale"] == 0.5 and cfg["camera"]["max_exposure_us"] == 3000
