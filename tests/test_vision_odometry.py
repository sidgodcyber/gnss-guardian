"""Camera geometry, optical flow and visual odometry on rendered ground with known motion.

Rendered frames: procedural soil/gravel texture, downward pinhole camera 1 m up (OV5647 nominal
focal 635 px at 640 px), pole sway 1.5 deg, height bob 3 cm, sensor noise. Real footage will drift
more (lens distortion, uneven ground, tilt); these tests pin the conventions and the algorithm.
"""
import math

import numpy as np
import pytest
from conftest import synth_walk

from gnss_guardian.vision import geometry as geo
from gnss_guardian.vision.flow import FlowTracker
from gnss_guardian.vision.odometry import VisualOdometry, wrap180

Pose, Renderer, Trajectory, frames = synth_walk.Pose, synth_walk.Renderer, synth_walk.Trajectory, synth_walk.frames


def test_geometry_focal_scale_and_frames(cfg_copy):
    assert abs(geo.focal_px(cfg_copy) - 635.5) < 1.0                      # 53.5 deg HFOV at 640 px
    assert abs(geo.metres_per_px(cfg_copy) - 1 / 635.5) < 1e-5
    cfg_copy["camera"]["focal_px"] = 700.0
    assert geo.focal_px(cfg_copy, 320) == 350.0                           # calibrated value scales with width
    for psi in (0, 37, 90, 211):
        e, n = geo.body_to_world(1.5, -2.0, psi)
        assert np.allclose(geo.world_to_body(e, n, psi), (1.5, -2.0))
    assert np.allclose(geo.body_to_world(0, 1, 90), (1, 0), atol=1e-12)   # forward at heading 90 = east
    m = np.array([[1.0, 0.0, -10.0], [0.0, 1.0, 0.0]])                    # ground moved 10 px left
    r, f, d = geo.camera_offset_from_match(m, (640, 480), 0.01)
    assert np.allclose((r, f, d), (-0.1, 0.0, 0.0))                       # camera A was 0.1 m left of B


def test_flow_translation_rotation_and_low_texture(cfg):
    r, mpp = Renderer(), geo.metres_per_px(cfg)
    ft = FlowTracker(cfg)
    ft.step(r.frame(Pose(0, 0, 0, 0, 1.0)))
    res = ft.step(r.frame(Pose(0, 0.05, 0.0, 0, 1.0)))                    # 5 cm east, facing north
    right, fwd, dpsi = geo.camera_offset_from_match(res.m, (640, 480), mpp)
    assert res.ok and res.quality > 0.8 and abs(right + 0.05) < 0.002 and abs(fwd) < 0.002
    ft.reset()
    ft.step(r.frame(Pose(0, 0, 0, 0, 1.0)))
    res = ft.step(r.frame(Pose(0, 0, 0, 5, 1.0)))                         # turned 5 deg right
    assert abs(geo.camera_offset_from_match(res.m, (640, 480), mpp)[2] + 5) < 0.05
    flat = Renderer(flat=True)
    vo = VisualOdometry(cfg)
    flags = [vo.step(k / 30, flat.frame(Pose(k / 30, 0.04 * k, 0, 0, 1.0)))["flag"] for k in range(40)]
    assert "vision_lost" in flags and vo.last["quality"] < cfg["vision"]["low_quality"]
    assert math.hypot(vo.e, vo.n) < 0.2                                   # no invented motion


@pytest.mark.parametrize("heading", [90, 30])
def test_straight_10m_drift(cfg, heading):
    vo = VisualOdometry(cfg)
    vo.heading, vo.aligned = heading, True
    for t, p, img in frames(Trajectory(straight=f"10:1.2:{heading}"), Renderer(), 10 / 1.2 + 0.5, 30):
        vo.step(t, img)
    err = math.hypot(vo.e - p.east, vo.n - p.north)
    print(f"straight 10 m at {heading} deg: drift {err:.3f} m ({10 * err:.1f} % )")
    assert err < 0.2 and abs(wrap180(vo.heading - p.heading)) < 1


def test_loop_with_corners_and_bridged_frames(cfg):
    vo = VisualOdometry(cfg)
    vo.heading, vo.aligned = 90.0, True
    worst = 0.0
    flat = Renderer(flat=True).frame(Pose(0, 0, 0, 0, 1.0))
    for k, (t, p, img) in enumerate(frames(Trajectory(walk="loop:20:10:1.2"), Renderer(), 32 / 1.2, 20)):
        if 335 <= k <= 337:                                               # three useless frames in the first corner
            img = flat
        if vo.step(t, img)["flag"] is None:                               # skipped frames wait for the bridge
            worst = max(worst, abs(wrap180(vo.heading - p.heading)))
    err = math.hypot(vo.e - p.east, vo.n - p.north)
    print(f"32 m, two corners: end error {err:.2f} m, worst heading error {worst:.1f} deg")
    assert err < 0.6 and worst < 3


def test_align_to_course_learns_mount_yaw(cfg):
    vo = VisualOdometry(cfg)                                              # starts at heading 0, camera turned 30 deg
    traj = Trajectory(straight="10:1.2:90", mount_yaw_deg=30)
    for t, p, img in frames(traj, Renderer(), 3, 30):
        vo.step(t, img)
    assert vo.align_to_course(90.0, 1.0) is not None and vo.aligned
    assert abs(wrap180(vo.heading - 120)) < 2                             # camera heading = course + mount yaw
    _, ve, vn, _ = vo.velocity()
    assert abs(math.degrees(math.atan2(ve, vn)) - 90) < 2
