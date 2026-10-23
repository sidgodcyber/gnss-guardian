"""Vision frame-rate benchmark, to run the same measurement on the laptop and on the Pi:

    gnss-guardian vision-bench [--video FILE] [--frames 300]

Without a video, frames are a random ground texture shifted 30 px and turned 0.3 deg per frame
(a 1.4 m/s walk at 30 fps, 1 m up). Reports odometry frames per second (flow + integration, the
per-frame cost) and the ORB keyframe cost (paid every keyframes.match_every_n_frames frames during
vision navigation and once per keyframe while GNSS is trusted).
"""
from __future__ import annotations

import time

import cv2
import numpy as np

from gnss_guardian.vision.odometry import VisualOdometry
from gnss_guardian.vision.terrain import TerrainMap


def synthetic_frames(n: int, w: int = 640, h: int = 480, seed: int = 1) -> list[np.ndarray]:
    rng = np.random.default_rng(seed)
    big = 3000
    fine = cv2.GaussianBlur(rng.random((big, big), dtype=np.float32), (0, 0), 1.0)
    blobs = cv2.resize(rng.random((200, 200), dtype=np.float32), (big, big), interpolation=cv2.INTER_CUBIC)
    tex = (30 + 190 * np.clip(0.5 * fine + 0.5 * blobs, 0, 1)).astype(np.uint8)
    out = []
    for k in range(n):
        x0, y0 = 300 + (30 * k) % (big - w - 600), (big - h) // 2       # a window walking across the texture
        m = cv2.getRotationMatrix2D((x0 + w / 2, y0 + h / 2), 0.3 * k, 1.0)
        m[0, 2] -= x0
        m[1, 2] -= y0
        out.append(cv2.warpAffine(tex, m, (w, h), flags=cv2.INTER_LINEAR))
    return out


def run(cfg: dict, frames: list[np.ndarray] | None = None, n: int = 300, fps: float = 30.0) -> dict:
    frames = frames or synthetic_frames(n, cfg["camera"]["width"], cfg["camera"]["height"])
    vo = VisualOdometry(cfg, (frames[0].shape[1], frames[0].shape[0]))
    t0 = time.perf_counter()
    for k, img in enumerate(frames):
        vo.step(k / fps, img)
    el = time.perf_counter() - t0
    tm = TerrainMap(cfg, vo.size, vo.mpp)
    sample = frames[:: max(1, len(frames) // 20)][:20]
    t1 = time.perf_counter()
    for img in sample:
        tm._features(img)
    orb_ms = 1000 * (time.perf_counter() - t1) / len(sample)
    every = cfg["keyframes"]["match_every_n_frames"]
    flow_ms = 1000 * el / len(frames)
    return {"frames": len(frames), "size": f"{vo.size[0]}x{vo.size[1]}", "process_scale": cfg["vision"]["process_scale"],
            "opencv_threads": cv2.getNumThreads(), "odometry_fps": round(len(frames) / el, 1),
            "odometry_ms": round(flow_ms, 2), "orb_ms": round(orb_ms, 1),
            "vision_nav_fps": round(1000 / (flow_ms + orb_ms / every), 1)}
