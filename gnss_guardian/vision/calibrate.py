"""Fit the camera scale from a walk of known length.

    gnss-guardian calibrate --video walk10m/video.avi --distance 10 [--height 1.0] [--dry-run]
    gnss-guardian calibrate --live --distance 5 [--camera picam]     # on the Pi, on the road, Enter / Enter

Walk (or push the trolley) along a measured straight line (tape, 5 or 10 m) with the camera at its
working height, standing still for a moment at both ends. --live runs the camera on the Pi itself:
Enter at the start mark, push, Enter at the end mark. The odometry measures the walk with the
current focal length; the focal length is scaled so the measured straight-line displacement equals
the true distance:

    focal_new = focal_current x measured_m / true_m

The straight-line (net) displacement is used, not the path length, because pole sway adds
back-and-forth motion that does not move the rig. The result goes to config/calibration.yaml
(merged over default.yaml at load), as camera.focal_px at camera.width and the height it was fitted at.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path

import yaml

from gnss_guardian.config import ROOT
from gnss_guardian.vision import geometry
from gnss_guardian.vision.camera import CvCamera
from gnss_guardian.vision.odometry import VisualOdometry


def calibrate(video: str | Path, distance_m: float, cfg: dict, height_m: float | None = None) -> dict:
    cam = cfg["camera"]
    # frames.csv timestamps if present; otherwise any start time will do (only displacement matters)
    src = CvCamera(str(video), cam["width"], cam["height"], start=datetime(2000, 1, 1, tzinfo=timezone.utc))
    try:
        return calibrate_frames(iter(src.read, None), distance_m, cfg, height_m, str(video))
    finally:
        src.close()


def calibrate_frames(frames, distance_m: float, cfg: dict, height_m: float | None = None, source: str = "",
                     progress=None) -> dict:
    """The fit over any sequence of camera Frames (a video, or the live camera until Enter)."""
    cam = cfg["camera"]
    if height_m is not None:
        cfg = {**cfg, "camera": {**cam, "height_m": height_m}}
    vo = VisualOdometry(cfg)
    n = low = 0
    first = None
    for fr in frames:
        st = vo.step(fr.t, fr.gray)
        n += 1
        low += st["flag"] in ("low_texture", "vision_lost")
        if first is None:
            first = (vo.e, vo.n)
        if progress is not None and n % 15 == 0:
            progress(math.hypot(vo.e - first[0], vo.n - first[1]), n)
    video = source
    if n < 10:
        raise ValueError(f"{video}: only {n} frames")
    net = math.hypot(vo.e - first[0], vo.n - first[1])
    if net < 0.5 * distance_m:
        raise ValueError(f"measured only {net:.2f} m for a {distance_m} m walk: low texture or wrong video?")
    f0 = geometry.focal_px(cfg)
    f1 = f0 * net / distance_m
    hfov = math.degrees(2 * math.atan(cam["width"] / 2 / f1))
    return {"focal_px": round(f1, 2), "previous_focal_px": round(f0, 2), "hfov_deg_equivalent": round(hfov, 2),
            "measured_m": round(net, 3), "path_m": round(vo.distance_m, 3), "true_m": distance_m,
            "straightness": round(net / vo.distance_m, 3) if vo.distance_m else None,
            "height_m": cfg["camera"]["height_m"], "frames": n, "low_quality_frames": low,
            "video": str(video)}


def write_calibration(res: dict, cfg: dict, path: str | Path | None = None) -> Path:
    path = Path(path) if path else ROOT / cfg["camera"]["calibration_file"]
    path.parent.mkdir(parents=True, exist_ok=True)
    head = (f"# Written by `gnss-guardian calibrate` on {datetime.now(timezone.utc):%Y-%m-%dT%H:%MZ}\n"
            f"# {res['video']}: {res['true_m']} m walk measured {res['measured_m']} m with focal "
            f"{res['previous_focal_px']} px -> {res['focal_px']} px (HFOV equivalent {res['hfov_deg_equivalent']} deg),\n"
            f"# path {res['path_m']} m, straightness {res['straightness']}, {res['frames']} frames, "
            f"{res['low_quality_frames']} low-quality.\n")
    body = yaml.safe_dump({"camera": {"focal_px": res["focal_px"], "height_m": res["height_m"]}}, sort_keys=False)
    path.write_text(head + body, encoding="utf-8")
    return path
