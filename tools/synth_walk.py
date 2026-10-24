"""Synthetic downward-camera walks over a procedural ground texture, with known motion: the ground
truth for testing vision odometry, keyframes and calibration without footage.

    python tools/synth_walk.py OUT_DIR [--walk loop:20:10:1.2 | outback:12:90:1.2 | --straight 10:1.2:90]
                                       [--duration-s 60] [--fps 30] [--sway-deg 1.5] [--bob-m 0.03]
                                       [--noise 2] [--mount-yaw-deg 0] [--true-focal-px 635]
                                       [--start 2026-10-22T14:00:00Z] [--nmea] [--world E0:E1:N0:N1]

writes OUT_DIR/video.avi (MJPG, grey), OUT_DIR/frames.csv (idx, host_utc, as tools/record_session.py
does on the Pi), OUT_DIR/truth.csv (t, east, north, heading, height) and with --nmea a matching
OUT_DIR/nmea.nmea from tools/fake_l86.py (same walk, same clock), so the whole stack can be replayed.

Camera model: pinhole pointing straight down, image up = camera forward, focal --true-focal-px at
640 px width (OV5647 nominal 53.5 deg HFOV -> 635 px). Perturbations: pole sway (tilt, --sway-deg at
0.9 Hz, seen as a ground shift of h tan(tilt)), height bob (--bob-m at step frequency 1.8 Hz),
sensor noise (--noise grey levels), brightness flicker (+/-3 %). The ground is a 3 mm-per-texel
mix of fine grain, 1.5 cm blobs and 10 cm patches (soil/gravel-like), 24 x 24 m around the start.
"""
from __future__ import annotations

import argparse
import functools
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np

TEXEL_M = 0.003
WORLD_M = 24.0


@functools.lru_cache(maxsize=4)
def ground_texture(seed: int = 7, texel_m: float = TEXEL_M, world_m: float = WORLD_M, flat: bool = False,
                   shape_m: tuple[float, float] | None = None) -> np.ndarray:
    """world_m x world_m square, or a shape_m = (east, north) metres strip (a road for a long walk)."""
    nx, ny = (int(world_m / texel_m),) * 2 if shape_m is None else (int(shape_m[0] / texel_m), int(shape_m[1] / texel_m))
    rng = np.random.default_rng(seed)
    if flat:                                             # low texture: smooth grey with faint noise
        return np.clip(128 + rng.normal(0, 1.0, (ny, nx)), 0, 255).astype(np.uint8)
    def octave(cell_m, sigma):
        wx, wy = (world_m, world_m) if shape_m is None else shape_m
        kx, ky = max(2, int(wx / cell_m)), max(2, int(wy / cell_m))
        small = rng.random((ky, kx), dtype=np.float32)
        big = cv2.resize(small, (nx, ny), interpolation=cv2.INTER_CUBIC)
        return cv2.GaussianBlur(big, (0, 0), sigma) if sigma else big
    fine = cv2.GaussianBlur(rng.random((ny, nx), dtype=np.float32), (0, 0), 1.0)
    tex = 0.40 * fine + 0.40 * octave(0.015, 0) + 0.20 * octave(0.10, 0)
    tex = (tex - tex.min()) / (tex.max() - tex.min())
    return (30 + 190 * tex).astype(np.uint8)


@dataclass
class Pose:
    t: float
    east: float
    north: float
    heading: float        # deg, camera forward axis, clockwise from north
    height: float
    tilt_r: float = 0.0   # deg, ground shift to the right = h tan(tilt_r)
    tilt_f: float = 0.0


class Renderer:
    def __init__(self, width=640, height=480, focal_px=635.0, noise=2.0, seed=7, flat=False,
                 world: tuple[float, float, float, float] | None = None):
        """world = (east_min, east_max, north_min, north_max) metres of ground (default the 24 m square
        around the start); beyond it the texture is mirrored, which reverses the apparent motion."""
        self.w, self.h, self.f, self.noise = width, height, focal_px * width / 640, noise
        if world is None:
            self.tex = ground_texture(seed, flat=flat)
            half = self.tex.shape[0] * TEXEL_M / 2
            self.e0, self.n1 = -half, half
        else:
            self.tex = ground_texture(seed, flat=flat, shape_m=(world[1] - world[0], world[3] - world[2]))
            self.e0, self.n1 = world[0], world[3]
        u, v = np.meshgrid(np.arange(width, dtype=np.float32), np.arange(height, dtype=np.float32))
        self.du, self.dv = u - width / 2 + 0.5, height / 2 - v - 0.5
        self.rng = np.random.default_rng(seed + 1)

    def frame(self, p: Pose) -> np.ndarray:
        mpp = p.height / self.f
        right = self.du * mpp + p.height * math.tan(math.radians(p.tilt_r))
        fwd = self.dv * mpp + p.height * math.tan(math.radians(p.tilt_f))
        c, s = math.cos(math.radians(p.heading)), math.sin(math.radians(p.heading))
        e = p.east + right * c + fwd * s
        n = p.north - right * s + fwd * c
        mx = ((e - self.e0) / TEXEL_M).astype(np.float32)
        my = ((self.n1 - n) / TEXEL_M).astype(np.float32)
        img = cv2.remap(self.tex, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        gain = 1.0 + 0.03 * math.sin(2 * math.pi * 0.3 * p.t)
        out = img.astype(np.float32) * gain
        if self.noise:
            out += self.rng.normal(0, self.noise, out.shape).astype(np.float32)
        return np.clip(out, 0, 255).astype(np.uint8)


def walk_at(spec: str | None, s: float):
    """Same rectangle walk as tools/fake_l86.py (east, north, west, south from the start point)."""
    import importlib.util
    import sys
    if "fake_l86" not in sys.modules:
        spec_ = importlib.util.spec_from_file_location("fake_l86", Path(__file__).with_name("fake_l86.py"))
        mod = importlib.util.module_from_spec(spec_)
        sys.modules["fake_l86"] = mod
        spec_.loader.exec_module(mod)
    return sys.modules["fake_l86"].walk_at(spec, s)


class Trajectory:
    """Poses at time t for a walk: position from the path, camera heading = travel direction
    (rate-limited to turn_rate_dps, so corners take ~1 s) + mount yaw, plus sway and height bob."""

    def __init__(self, walk: str | None = None, straight: str | None = None, height_m=1.0, sway_deg=1.5,
                 bob_m=0.03, mount_yaw_deg=0.0, turn_rate_dps=90.0, start_s: float = 0.0, still_s: float = 0.0):
        self.walk, self.h, self.sway, self.bob = walk, height_m, sway_deg, bob_m
        self.mount, self.turn_rate, self.still_s = mount_yaw_deg, turn_rate_dps, still_s
        self.straight = tuple(float(x) for x in straight.split(":")) if straight else None   # dist:speed:heading
        self._psi, self._t = None, None

    def _path(self, t):
        tm = max(0.0, t - self.still_s)                   # stand still first (warm-up, home)
        if self.straight:
            dist, v, hd = self.straight
            d = min(dist, v * tm)
            e, n = d * math.sin(math.radians(hd)), d * math.cos(math.radians(hd))
            moving = v * tm < dist and t >= self.still_s
            return e, n, (v * math.sin(math.radians(hd)) if moving else 0.0), (v * math.cos(math.radians(hd)) if moving else 0.0)
        if t < self.still_s:
            e, n, ve, vn = walk_at(self.walk, 0.0)
            return e, n, 0.0, 0.0
        return walk_at(self.walk, tm)

    def pose(self, t: float) -> Pose:
        e, n, ve, vn = self._path(t)
        if math.hypot(ve, vn) > 0.05:
            target = math.degrees(math.atan2(ve, vn))
        elif self.straight:
            target = self.straight[2]
        else:
            target = self._psi if self._psi is not None else math.degrees(math.atan2(*walk_at(self.walk, 0.5)[2:]))
        if self._psi is None:
            self._psi = target
        else:
            step = self.turn_rate * max(t - self._t, 0.0)
            d = (target - self._psi + 180) % 360 - 180
            self._psi += max(-step, min(step, d))
        self._t = t
        moving = math.hypot(ve, vn) > 0.05
        sw = self.sway if moving else self.sway * 0.2
        return Pose(t, e, n, (self._psi + self.mount) % 360,
                    self.h + (self.bob * math.sin(2 * math.pi * 1.8 * t) if moving else 0.0),
                    sw * math.sin(2 * math.pi * 0.9 * t), sw * math.sin(2 * math.pi * 0.9 * t + 1.3))


def frames(traj: Trajectory, renderer: Renderer, duration_s: float, fps: float):
    """(t, Pose, grey image) for t = 0, 1/fps, ..."""
    for k in range(int(round(duration_s * fps))):
        t = k / fps
        p = traj.pose(t)
        yield t, p, renderer.frame(p)


def write_session(out: Path, traj: Trajectory, renderer: Renderer, duration_s: float, fps: float,
                  start: datetime) -> int:
    out.mkdir(parents=True, exist_ok=True)
    vw = cv2.VideoWriter(str(out / "video.avi"), cv2.VideoWriter_fourcc(*"MJPG"), fps,
                         (renderer.w, renderer.h), isColor=False)
    vw.set(cv2.VIDEOWRITER_PROP_QUALITY, 95)
    n = 0
    with open(out / "frames.csv", "w", encoding="ascii") as fc, open(out / "truth.csv", "w", encoding="ascii") as ft:
        fc.write("idx,host_utc\n")
        ft.write("t,east,north,heading,height\n")
        for t, p, img in frames(traj, renderer, duration_s, fps):
            vw.write(img)
            host = start + timedelta(seconds=t)
            fc.write(f"{n},{host:%Y-%m-%dT%H:%M:%S}.{host.microsecond // 1000:03d}Z\n")
            ft.write(f"{t:.4f},{p.east:.4f},{p.north:.4f},{p.heading:.3f},{p.height:.4f}\n")
            n += 1
    vw.release()
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    ap.add_argument("--walk", default=None)
    ap.add_argument("--straight", default=None, help="dist_m:speed_mps:heading_deg")
    ap.add_argument("--duration-s", type=float, default=60)
    ap.add_argument("--still-s", type=float, default=0, help="stand still this long first")
    ap.add_argument("--fps", type=float, default=30)
    ap.add_argument("--sway-deg", type=float, default=1.5)
    ap.add_argument("--bob-m", type=float, default=0.03)
    ap.add_argument("--noise", type=float, default=2.0)
    ap.add_argument("--mount-yaw-deg", type=float, default=0.0)
    ap.add_argument("--true-focal-px", type=float, default=635.0)
    ap.add_argument("--height-m", type=float, default=1.0)
    ap.add_argument("--start", default="2026-10-22T14:00:00Z")
    ap.add_argument("--nmea", action="store_true", help="also write nmea.nmea for the same walk (fake_l86)")
    ap.add_argument("--nmea-args", default="", help="extra fake_l86 arguments, e.g. '--spoof drift --spoof-cn0 natural'")
    ap.add_argument("--world", default=None, help="E0:E1:N0:N1 metres of ground (default 24 m square), e.g. -4:34:-3:3")
    a = ap.parse_args(argv)
    start = datetime.fromisoformat(a.start.replace("Z", "+00:00")).astimezone(timezone.utc)
    traj = Trajectory(a.walk, a.straight, a.height_m, a.sway_deg, a.bob_m, a.mount_yaw_deg, still_s=a.still_s)
    out = Path(a.out)
    world = tuple(float(x) for x in a.world.split(":")) if a.world else None
    n = write_session(out, traj, Renderer(focal_px=a.true_focal_px, noise=a.noise, world=world), a.duration_s, a.fps,
                      start)
    print(f"wrote {out}: {n} frames")
    if a.nmea:
        walk_at(None, 0)                                  # loads fake_l86
        import sys
        fk = sys.modules["fake_l86"]
        argv2 = ["--write", str(out / "nmea.nmea"), "--start", a.start, "--duration-s", str(int(a.duration_s)),
                 "--still-s", str(a.still_s)]
        if a.walk:
            argv2 += ["--walk", a.walk]
        fa = fk.build_parser().parse_args(argv2 + a.nmea_args.split())
        fk.write_file(fa, fk.load_cycles(fk.FIXTURE), start)
        print(f"wrote {out / 'nmea.nmea'}")


if __name__ == "__main__":
    main()
