"""Live camera overlay: the processed frame with what the module is doing, drawn in its own thread
at a few frames per second so it never slows the vision loop.

    ov = OverlayThread(cfg, hub, nav, record=out_dir / "overlay.mp4", camera_fps=lambda: cam.fps)
    ov.start() ... ov.stop.set()

On the frame (320x240): tracked corners and their flow (magnified), state, position source (GNSS or
vision), displacement from home in metres and its bearing, camera heading, keyframe count, a
"HOME MATCH" marker for a fresh strict home-keyframe match, "DEMO INJECTION ACTIVE" while a demo
spoof is injected, and an inset map (metres from home) of the vision track against the GNSS track
with the keyframes and home. Each frame is JPEG-encoded for the MJPEG stream (LiveHub) and, with
--overlay-record, appended to overlay.mp4, its time (the epochs' clock) to overlay_frames.csv so the
dashboard can show it in step with the session (gnss-guardian replay SESSION_FOLDER).

Rate: overlay.fps, lowered by one (down to overlay.min_fps) whenever the camera thread's measured
rate falls below overlay.protect_vision_frac of the camera's own frame rate: the overlay gives way,
the vision does not.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger("gnss_guardian.overlay")

W, H = 320, 240
STATE_COLOUR = {"GNSS_OK": (80, 200, 80), "WARMING_UP": (160, 160, 160), "SUSPECT": (0, 200, 255),
                "JAMMED": (0, 80, 255), "SPOOFED": (60, 60, 255), "VISION_NAV": (255, 180, 0),
                "RETURN_HOME": (255, 120, 0), "HOME_REACHED": (80, 255, 80)}


def _text(img, s, org, scale=0.42, colour=(255, 255, 255), thick=1):
    cv2.putText(img, s, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), thick + 2, cv2.LINE_AA)
    cv2.putText(img, s, org, cv2.FONT_HERSHEY_SIMPLEX, scale, colour, thick, cv2.LINE_AA)


def render(snap: dict | None, view: dict | None, track: dict, src_size=(640, 480), match_max_age_s: float = 3.0,
           ) -> np.ndarray:
    """One overlay frame (BGR, W x H) from a VisionNav.overlay_snapshot, the live view and the map tracks."""
    gray = snap.get("gray") if snap else None
    if gray is not None:
        img = cv2.cvtColor(cv2.resize(gray, (W, H), interpolation=cv2.INTER_AREA), cv2.COLOR_GRAY2BGR)
        sx, sy = W / gray.shape[1], H / gray.shape[0]
    else:
        img = np.full((H, W, 3), 30, np.uint8)
        _text(img, "no camera", (W // 2 - 40, H // 2), 0.6, (180, 180, 180))
        sx, sy = W / src_size[0], H / src_size[1]
    pairs = snap.get("pairs") if snap else None
    if pairs is not None:
        a, b = pairs
        for (x0, y0), (x1, y1) in zip(a[::2], b[::2]):
            p1 = (int(x1 * sx), int(y1 * sy))
            p0 = (int(p1[0] + 3 * (x0 - x1) * sx), int(p1[1] + 3 * (y0 - y1) * sy))     # flow x3, toward the past
            cv2.line(img, p0, p1, (0, 220, 255), 1, cv2.LINE_AA)
            cv2.circle(img, p1, 2, (0, 255, 0), -1)
    v = view or {}
    state = v.get("state") or "WAITING"
    col = STATE_COLOUR.get(state, (200, 200, 200))
    cv2.rectangle(img, (0, 0), (W, 34), (0, 0, 0), -1)
    _text(img, state, (4, 14), 0.5, col, 1)
    fu = v.get("fused")
    src = {"gnss": "GNSS", "vision": "VISION", "vision_stale": "VISION (lost)", "last_fix": "LAST FIX"}.get(
        fu["source"], fu["source"]) if fu else "-"
    _text(img, f"pos: {src}", (150, 14), 0.42)
    if fu:
        _text(img, f"home {fu['home_distance_m']:.1f} m @ {fu['home_bearing_deg']:.0f} deg   "
                   f"E {fu['e']:+.1f} N {fu['n']:+.1f}", (4, 29), 0.38)
    hd = snap.get("heading") if snap else None
    kf = snap.get("keyframes") if snap else None
    _text(img, f"hdg {'-' if hd is None else f'{hd:.0f}'}{'' if snap and snap.get('aligned') else '?'}  "
               f"kf {'-' if kf is None else kf}", (4, 48), 0.38)
    hm = snap.get("home_match") if snap else None
    if hm and snap.get("t") is not None and snap["t"] - hm["t"] <= match_max_age_s:
        cv2.rectangle(img, (1, 1), (W - 2, H - 2), (80, 255, 80), 3)
        _text(img, f"HOME MATCH {hm['inliers']} inl", (W - 150, 48), 0.45, (80, 255, 80), 1)
    if snap and snap.get("flag") == "vision_lost":
        _text(img, "VISION LOST", (W // 2 - 50, H // 2), 0.6, (0, 0, 255), 2)
    inj = (v.get("injection") or {}).get("active")
    if inj:
        cv2.rectangle(img, (0, H - 22), (W, H), (0, 0, 200), -1)
        _text(img, "DEMO INJECTION ACTIVE", (6, H - 6), 0.5, (255, 255, 255), 1)
    _inset(img, track, fu, y_bottom=H - (24 if inj else 2))
    return img


def _inset(img, track: dict, fu, size: int = 96, y_bottom: int = H - 2) -> None:
    """Map in metres from home: GNSS track blue, vision track orange, keyframes white, home green."""
    x0, y0 = W - size - 2, y_bottom - size
    roi = img[y0:y0 + size, x0:x0 + size]
    roi[:] = (roi * 0.35).astype(np.uint8)
    pts = track["gnss"][-300:] + track["vision"][-300:] + [(e, n) for e, n, _ in track["keyframes"]]
    if fu:
        pts.append((fu["e"], fu["n"]))
    ext = max([10.0] + [max(abs(e), abs(n)) * 1.15 for e, n in pts])
    k = (size / 2 - 3) / ext

    def px(e, n):
        return int(x0 + size / 2 + e * k), int(y0 + size / 2 - n * k)
    for name, colour in (("gnss", (255, 140, 40)), ("vision", (0, 165, 255))):
        p = np.array([px(e, n) for e, n in track[name][-300:]], np.int32)
        if len(p) > 1:
            cv2.polylines(img, [p], False, colour, 1, cv2.LINE_AA)
    for e, n, home in track["keyframes"]:
        if not home:
            cv2.circle(img, px(e, n), 1, (255, 255, 255), -1)
    hx, hy = px(0, 0)
    cv2.drawMarker(img, (hx, hy), (80, 255, 80), cv2.MARKER_CROSS, 8, 2)
    if fu:
        cv2.circle(img, px(fu["e"], fu["n"]), 3, (0, 0, 255), -1)
    cv2.rectangle(img, (x0, y0), (x0 + size - 1, y0 + size - 1), (200, 200, 200), 1)
    _text(img, f"{ext:.0f} m", (x0 + 2, y0 + 10), 0.3)


class OverlayThread(threading.Thread):
    def __init__(self, cfg: dict, hub, nav=None, record: str | Path | None = None, camera_fps=None,
                 camera_target_fps: float | None = None, clock=time.monotonic):
        super().__init__(daemon=True, name="overlay")
        o = cfg["overlay"]
        self.cfg, self.hub, self.nav = cfg, hub, nav
        self.fps, self.min_fps, self.protect = float(o["fps"]), float(o["min_fps"]), o["protect_vision_frac"]
        self.quality = int(o["jpeg_quality"])
        self.camera_fps = camera_fps or (lambda: None)
        self.cam_target = camera_target_fps or cfg["camera"]["fps"]
        self.record = Path(record) if record else None
        self.clock = clock
        self.writer = None
        self._times = None
        self.stop = threading.Event()
        self.frames = 0
        self.render_ms: float | None = None
        self.error: str | None = None
        self.events: list[tuple[str, dict]] = []
        self._last_check = time.monotonic()

    def _open_writer(self):
        for path, fourcc in ((self.record, "mp4v"), (self.record.with_suffix(".avi"), "MJPG")):
            w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*fourcc), self.fps, (W, H), True)
            if w.isOpened():
                self.events.append(("overlay_record", {"path": str(path), "fps": self.fps}))
                self._times = open(self.record.with_name("overlay_frames.csv"), "w", encoding="ascii")
                self._times.write("idx,t_mono\n")
                return w
            w.release()
        self.events.append(("overlay_record_failed", {"path": str(self.record)}))
        return None

    def _adapt(self) -> None:
        """Every 10 s: if the camera thread runs below protect_vision_frac of the source rate, slow the overlay."""
        if time.monotonic() - self._last_check < 10:
            return
        self._last_check = time.monotonic()
        cam = self.camera_fps()
        if cam is not None and cam < self.protect * self.cam_target and self.fps > self.min_fps:
            self.fps = max(self.min_fps, self.fps - 1)
            self.events.append(("overlay_fps", {"fps": self.fps, "vision_fps": cam}))
            log.info("vision at %.1f fps: overlay lowered to %.0f fps", cam, self.fps)

    def run(self):
        try:
            if self.record:
                self.writer = self._open_writer()
            while not self.stop.is_set():
                t0 = time.perf_counter()
                snap = self.nav.overlay_snapshot() if self.nav is not None else None
                img = render(snap, self.hub.view(), self.hub.map_snapshot(),
                             match_max_age_s=self.cfg["state"]["home_match_max_age_s"])
                ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
                if ok:
                    self.hub.publish_jpeg(jpg.tobytes())
                if self.writer is not None:
                    self.writer.write(img)
                    self._times.write(f"{self.frames},{self.clock():.3f}\n")
                self.frames += 1
                el = time.perf_counter() - t0
                self.render_ms = round(1000 * el, 1)
                self._adapt()
                self.stop.wait(max(0.0, 1.0 / self.fps - el))
        except Exception as e:                           # display only: never take live mode down
            self.error = repr(e)
            log.exception("overlay thread stopped")
        finally:
            if self.writer is not None:
                self.writer.release()
                self._times.close()
