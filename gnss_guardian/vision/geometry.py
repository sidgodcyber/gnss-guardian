"""Downward camera over flat ground: pixels <-> metres, camera body frame <-> local east/north.

Conventions (pinned by the synthetic tests):
  image      u right, v down, principal point at the image centre
  body       forward = image up (-v), right = image right (+u)
  heading    psi, degrees clockwise from north, of the camera's forward axis
  world      local east/north metres (from home)

A ground point at pixel (u, v) seen by a camera at (E0, N0) with heading psi and metres-per-pixel m:
  right = (u - cx) m,   forward = (cy - v) m
  E = E0 + right cos(psi) + forward sin(psi)
  N = N0 - right sin(psi) + forward cos(psi)
"""
from __future__ import annotations

import math

import numpy as np


def focal_px(cfg: dict, width: int | None = None) -> float:
    """Focal length in pixels at ``width`` (default camera.width): the calibrated value if any,
    else from the horizontal field of view."""
    cam = cfg["camera"]
    w = width or cam["width"]
    if cam.get("focal_px"):
        return float(cam["focal_px"]) * w / cam["width"]
    return (w / 2) / math.tan(math.radians(cam["hfov_deg"]) / 2)


def metres_per_px(cfg: dict, width: int | None = None, height_m: float | None = None) -> float:
    return (height_m if height_m is not None else cfg["camera"]["height_m"]) / focal_px(cfg, width)


def body_to_world(right: float, forward: float, psi_deg: float) -> tuple[float, float]:
    c, s = math.cos(math.radians(psi_deg)), math.sin(math.radians(psi_deg))
    return right * c + forward * s, -right * s + forward * c


def world_to_body(e: float, n: float, psi_deg: float) -> tuple[float, float]:
    c, s = math.cos(math.radians(psi_deg)), math.sin(math.radians(psi_deg))
    return e * c - n * s, e * s + n * c


def similarity_angle_deg(m: np.ndarray) -> float:
    """Rotation of a 2x3 similarity matrix, degrees, in image coordinates (v down: positive = clockwise on screen)."""
    return math.degrees(math.atan2(m[1, 0], m[0, 0]))


def similarity_scale(m: np.ndarray) -> float:
    return math.hypot(m[0, 0], m[1, 0])


def camera_offset_from_match(m: np.ndarray, size: tuple[int, int], mpp: float) -> tuple[float, float, float]:
    """For a similarity m mapping points of image A into image B (both from the downward camera):
    (right, forward, dpsi) = position of camera A in camera B's body frame (metres) and
    heading(A) - heading(B) in degrees.

    Camera A's centre pixel sees the ground point under camera A; m sends it to where that point
    appears in B, which in B's body frame is A's position. A rotation of +theta on screen (clockwise,
    v down) of the ground between A and B means B is turned theta counter-clockwise from A:
    heading(A) - heading(B) = theta.
    """
    w, h = size
    cx, cy = w / 2, h / 2
    pu = m[0, 0] * cx + m[0, 1] * cy + m[0, 2]
    pv = m[1, 0] * cx + m[1, 1] * cy + m[1, 2]
    return (pu - cx) * mpp, (cy - pv) * mpp, similarity_angle_deg(m)
