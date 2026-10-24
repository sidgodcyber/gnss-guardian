"""dashboard.json written by replay: state transitions in order, no coordinates, Pi recorder folders."""
import json

import pytest
from conftest import fast_vision_cfg, sim_file

from gnss_guardian.io.nmea import split_prefix
from gnss_guardian.run import replay, resolve_recording
from gnss_guardian.vision.camera import read_frames_csv


def run(tmp_path, cfg, name, *args, duration_s=300):
    nmea = sim_file(tmp_path / f"{name}.nmea", "--still-s", "90", *args, duration_s=duration_s)   # home needs a still rig
    replay(nmea, cfg, tmp_path / name)
    return json.loads((tmp_path / name / "dashboard.json").read_text(encoding="utf-8"))


def states(d):
    return [t["state"] for t in d["transitions"]]


def assert_no_coordinates(d):
    text = json.dumps(d)
    for key in ('"lat"', '"lon"', '"alt_m"', '"latitude"', '"longitude"'):
        assert key not in text
    assert "29.25" not in text and "79.53" not in text          # simulator home latitude / longitude


def test_jam_demo_transitions(tmp_path, cfg):
    d = run(tmp_path, cfg, "jam", "--jam", "ramp", "--jam-at", "120", "--recover-after", "40")
    assert states(d) == ["WARMING_UP", "GNSS_OK", "SUSPECT", "JAMMED", "GNSS_OK"]    # no camera: JAMMED until trusted
    t = [x["t"] for x in d["transitions"]]
    assert t == sorted(t)
    jammed = d["transitions"][3]
    assert 120 <= jammed["t"] <= 126 and "jamming.drop_frac" in jammed["indicators"] and jammed["cause"] == "jamming"
    assert d["transitions"][4]["reasons"] == ["detectors CLEAR: GNSS trusted again"]
    assert {r["state"] for r in d["timeline"]} == {"WARMING_UP", "GNSS_OK", "SUSPECT", "JAMMED"}   # read from epochs
    assert all(r["fused"]["source"] == "last_fix" for r in d["timeline"] if r["state"] == "JAMMED")
    assert d["meta"]["state_source"].startswith("pipeline state machine") and d["routes"] == []
    assert [(x["detector"], x["level"]) for x in d["detector_levels"]] == [
        ("jamming", "SUSPECT"), ("jamming", "ALARM"), ("jamming", "SUSPECT"), ("jamming", "CLEAR")]
    assert len(d["timeline"]) == 300 and d["meta"]["video"] is None
    assert any(r["gnss"]["e"] is not None for r in d["timeline"])        # local metres once home is set
    assert_no_coordinates(d)


def test_spoof_demo_transitions(tmp_path, cfg):
    d = run(tmp_path, cfg, "spoof", "--spoof", "jump", "--spoof-at", "120")
    assert states(d) == ["WARMING_UP", "GNSS_OK", "SPOOFED"]             # latched to the end of the file
    spoofed = d["transitions"][2]
    assert 118 <= spoofed["t"] <= 123 and "spoofing.speed" in spoofed["indicators"]
    assert all(i.startswith("spoofing.") for i in spoofed["indicators"])  # only detectors above CLEAR
    keys = {i["key"] for i in d["config"]["spoofing"]["indicators"]}
    assert keys == {"speed", "time", "altitude", "uniform_cn0", "newcomers", "vision"}
    assert d["config"]["hysteresis"]["alarm_on"] == cfg["detect"]["hysteresis"]["alarm_on"]
    assert_no_coordinates(d)


def test_vision_session_exports_video_and_track(tmp_path, cfg, synth_session):
    out = tmp_path / "synth"
    nmea, video, frames = resolve_recording(synth_session)
    replay(nmea, fast_vision_cfg(cfg), out, video=video, frames_csv=frames)
    d = json.loads((out / "dashboard.json").read_text(encoding="utf-8"))
    assert (out / "dashboard_video.webm").stat().st_size > 0
    assert d["meta"]["video"] == "dashboard_video.webm" and d["meta"]["video_fps"] > 0
    frames_idx = [r["frame"] for r in d["timeline"] if r["frame"] is not None]
    assert frames_idx == sorted(frames_idx) and len(frames_idx) > 0.9 * len(d["timeline"])
    assert any(r["vision"] and r["vision"]["e"] is not None for r in d["timeline"])
    assert d["keyframes"] and states(d) == ["WARMING_UP", "GNSS_OK"]
    assert_no_coordinates(d)


def test_pi_recorder_folder_replays_like_record_session(tmp_path, cfg):
    """gnss.nmea with t_mono,t_unix,$NMEA lines gives the same verdicts as the ISO-prefixed file."""
    src = sim_file(tmp_path / "ref.nmea", "--jam", "ramp", "--jam-at", "120", "--recover-after", "40", duration_s=200)
    pi = tmp_path / "pi_walk"
    pi.mkdir()
    lines = []
    for i, line in enumerate(src.read_text(encoding="ascii").splitlines()):
        _, host, sentence = split_prefix(line)
        lines.append(f"{1000 + i * 0.01:.3f},{host.timestamp():.3f},{sentence}")
    (pi / "gnss.nmea").write_text("\n".join(lines) + "\n", encoding="ascii")
    nmea, video, frames = resolve_recording(pi)
    assert nmea.name == "gnss.nmea" and video is None and frames is None
    a = replay(src, cfg, tmp_path / "s_ref")
    b = replay(nmea, cfg, tmp_path / "s_pi")
    assert a["epochs"] == b["epochs"] == 200
    da, db = (json.loads((tmp_path / s / "dashboard.json").read_text(encoding="utf-8")) for s in ("s_ref", "s_pi"))
    assert states(da) == states(db) == ["WARMING_UP", "GNSS_OK", "SUSPECT", "JAMMED", "GNSS_OK"]


def test_recording_folder_files_and_frames_csv(tmp_path):
    for name in ("gnss.nmea", "camera.avi"):
        (tmp_path / name).write_bytes(b"")
    (tmp_path / "frames.csv").write_text("idx,t_mono,t_unix\n0,10.0,1792677600.5\n1,10.05,1792677600.55\n")
    nmea, video, frames = resolve_recording(tmp_path)
    assert (nmea.name, video.name, frames.name) == ("gnss.nmea", "camera.avi", "frames.csv")
    assert read_frames_csv(frames) == [1792677600.5, 1792677600.55]
    (tmp_path / "frames.csv").write_text("idx,host_utc\n0,2026-10-22T14:00:00.500Z\n")
    assert read_frames_csv(frames) == [1792677600.5]
    (tmp_path / "frames.csv").write_text("idx,t_mono\n0,10.0\n")
    with pytest.raises(ValueError, match="host_utc or t_unix"):
        read_frames_csv(frames)
    with pytest.raises(FileNotFoundError):
        resolve_recording(tmp_path / "missing")
