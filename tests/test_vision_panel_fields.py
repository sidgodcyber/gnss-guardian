"""Read-only vision fields for the dashboard's vision pipeline panel: corners detected (Shi-Tomasi), points
tracked (Lucas-Kanade), RANSAC inliers vs outliers, image shift per frame, ORB fallback, distance since the
last trusted fix, keyframe / home matches, and the camera's pixels-to-metres inputs. Display only: the walk
below runs the same as without them (tests/test_vision_*.py cover the behaviour)."""
import json

from conftest import fast_vision_cfg, synth_walk

from gnss_guardian.run import replay, resolve_recording
from gnss_guardian.session import read_jsonl


def test_vision_pipeline_fields(tmp_path, cfg):
    synth_walk.main([str(tmp_path / "w"), "--walk", "loop:20:10:1.2", "--still-s", "30", "--duration-s", "75",
                     "--fps", "10", "--nmea", "--nmea-args", "--jam sudden --jam-at 45"])
    nmea, video, frames = resolve_recording(tmp_path / "w")
    replay(nmea, fast_vision_cfg(cfg), tmp_path / "s", video=video, frames_csv=frames)

    eps = read_jsonl(tmp_path / "s" / "epochs.jsonl")
    vis = [v["vision"] for v in eps if v.get("vision")]
    assert vis
    for x in vis:
        for k in ("n_corners", "n_tracked", "shift_px", "orb", "since_anchor_m", "last_match", "home_confirmed"):
            assert k in x, k
        assert x["n_inliers"] <= x["n_tracked"] or x["n_tracked"] == 0
    moving = [x for x in vis if x["shift_px"] is not None and x["shift_px"] > 0.5]
    assert moving and max(x["n_corners"] for x in vis) > 20
    print("corners", max(x["n_corners"] for x in vis), "tracked", max(x["n_tracked"] for x in vis),
          "shift px/frame", max(x["shift_px"] for x in moving))
    on_cam = [v for v in eps if (v.get("fused") or {}).get("source") == "vision"]
    assert on_cam and all(v["vision"]["since_anchor_m"] is not None for v in on_cam)

    d = json.loads((tmp_path / "s" / "dashboard.json").read_text(encoding="utf-8"))
    cam = d["config"]["camera"]
    assert cam["height_m"] > 0 and cam["focal_px"] > 0 and cam["width_px"] > 0
    rows = [r for r in d["timeline"] if r["vision"]]
    assert rows and all("n_corners" in r["vision"] and "since_anchor_m" in r["vision"] for r in rows)
