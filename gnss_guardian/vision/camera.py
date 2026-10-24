"""Frame sources: the Pi Camera (Picamera2, Raspberry Pi OS only), a USB webcam or a video file
(OpenCV). Every source yields grey frames at camera.width x camera.height with a timestamp on the
same clock as the GNSS epochs:

  live    time.monotonic() x clock_scale at capture (Pi camera, USB webcam, or a video file paced
          at its frame rate to stand in for a camera on the laptop)
  replay  host UTC seconds from the recording's frames.csv (tools/record_session.py), matching the
          host prefix of the recorded NMEA; without frames.csv, --video-start + idx / fps

    open_camera("auto" | "picam" | "usb:0" | "video:walk.avi", cfg, ...)
"""
from __future__ import annotations

import csv
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np


class CameraUnavailable(RuntimeError):
    pass


@dataclass
class Frame:
    idx: int
    t: float
    gray: np.ndarray


def _fit(gray: np.ndarray, w: int, h: int) -> np.ndarray:
    if gray.ndim == 3:
        gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
    if gray.shape[1] != w or gray.shape[0] != h:
        gray = cv2.resize(gray, (w, h), interpolation=cv2.INTER_AREA)
    return gray


class ExposureCap:
    """Keeps the shutter at or below max_us so a walk at ~1.5 m/s does not blur the ground (libcamera's
    auto exposure has no plain upper limit). Auto exposure runs until it chooses a longer shutter; then
    the shutter is fixed at max_us and the analogue gain servoes the mean brightness toward ``target``
    (up to max_gain); when the gain is back at 1 and the image is still bright, auto exposure returns
    (it will pick a shorter shutter by itself). Checked every ``every`` frames from the frame metadata."""

    def __init__(self, max_us: int, max_gain: float = 8.0, target: float = 110.0, every: int = 10):
        self.max_us, self.max_gain, self.target, self.every = max_us, max_gain, target, every
        self.manual = False
        self.gain = 1.0
        self.n = 0

    def step(self, exposure_us: float | None, gain: float | None, mean: float) -> dict | None:
        """Camera controls to apply now, or None."""
        self.n += 1
        if self.n % self.every or exposure_us is None:
            return None
        if not self.manual:
            if exposure_us <= self.max_us * 1.05:
                return None
            self.manual = True                      # keep the brightness: move the extra exposure into gain
            self.gain = min(self.max_gain, max(1.0, (gain or 1.0) * exposure_us / self.max_us))
            return {"AeEnable": False, "ExposureTime": int(self.max_us), "AnalogueGain": round(self.gain, 2)}
        if mean > 1.25 * self.target and self.gain <= 1.0:
            self.manual = False
            return {"AeEnable": True}
        new = min(self.max_gain, max(1.0, self.gain * (self.target / max(mean, 1.0)) ** 0.5))
        if abs(new - self.gain) / self.gain < 0.05:
            return None
        self.gain = new
        return {"AnalogueGain": round(new, 2)}


class PiCamera:
    """Pi Camera Rev 1.3 (OV5647) through Picamera2/libcamera: YUV420, the Y plane is the grey image.
    camera.max_exposure_us caps the shutter (ExposureCap); the shutter and gain in use are reported in
    ``exposure`` (live mode logs them as camera_exposure events)."""

    def __init__(self, width: int, height: int, fps: float, clock=time.monotonic, max_exposure_us: int | None = None):
        try:
            from picamera2 import Picamera2          # Raspberry Pi OS only (apt install python3-picamera2)
        except ImportError as e:
            raise CameraUnavailable("Picamera2 is only available on Raspberry Pi OS (sudo apt install "
                                    "python3-picamera2, venv with --system-site-packages); on a laptop use "
                                    "--camera usb:0 or --video FILE") from e
        self.w, self.h, self.clock, self.fps = width, height, clock, fps
        self.cam = Picamera2()
        frame_us = int(1e6 / fps)
        conf = self.cam.create_video_configuration(main={"size": (width, height), "format": "YUV420"},
                                                   controls={"FrameDurationLimits": (frame_us, frame_us)})
        self.cam.configure(conf)
        self.cam.start()
        self.idx = 0
        self.cap = ExposureCap(int(max_exposure_us)) if max_exposure_us else None
        self.exposure: dict = {"max_exposure_us": max_exposure_us, "exposure_us": None, "gain": None, "mode": "auto"}

    def read(self) -> Frame | None:
        req = self.cam.capture_request()
        try:
            arr = req.make_array("main")
            md = req.get_metadata()
        finally:
            req.release()
        t = self.clock()
        self.idx += 1
        gray = np.ascontiguousarray(arr[:self.h, :self.w])
        exp, gain = md.get("ExposureTime"), md.get("AnalogueGain")
        self.exposure.update(exposure_us=exp, gain=None if gain is None else round(gain, 2))
        if self.cap is not None:
            ctl = self.cap.step(exp, gain, float(gray[::8, ::8].mean()))
            if ctl:
                self.cam.set_controls(ctl)
                self.exposure["mode"] = "capped" if self.cap.manual else "auto"
        return Frame(self.idx - 1, t, gray)

    def close(self) -> None:
        self.cam.stop()
        self.cam.close()


def read_frames_csv(path: Path) -> list[float]:
    """Host UTC seconds per frame: column host_utc (ISO, tools/record_session.py) or t_unix (seconds,
    the standalone Pi recorders); t_mono (live overlay_frames.csv) is on the epochs' monotonic clock."""
    with open(path, encoding="ascii") as f:
        rows = csv.DictReader(f)
        cols = rows.fieldnames or []
        if "host_utc" in cols:
            return [datetime.fromisoformat(r["host_utc"].replace("Z", "+00:00")).timestamp() for r in rows]
        if "t_unix" in cols:
            return [float(r["t_unix"]) for r in rows]
        if "t_mono" in cols:
            return [float(r["t_mono"]) for r in rows]
    raise ValueError(f"{path}: needs a host_utc or t_unix column (found {', '.join(cols) or 'none'})")


class CvCamera:
    """USB webcam (index) or video file through OpenCV."""

    def __init__(self, src: int | str, width: int, height: int, clock=time.monotonic, frames_csv: str | Path | None = None,
                 start: datetime | None = None, pace: bool = False):
        self.cap = cv2.VideoCapture(src)
        if not self.cap.isOpened():
            raise CameraUnavailable(f"cannot open camera/video {src!r}")
        self.w, self.h, self.clock, self.pace = width, height, clock, pace
        self.file = isinstance(src, str)
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.times = None
        if self.file and not pace:
            csv_path = Path(frames_csv) if frames_csv else Path(src).with_name("frames.csv")
            if csv_path.exists():
                self.times = read_frames_csv(csv_path)
            elif start is not None:
                self.t0 = start.timestamp()
            else:
                raise CameraUnavailable(f"{src}: no frames.csv next to the video; give --video-start to place it in time")
        self.idx = 0
        self._wall0 = None

    def read(self) -> Frame | None:
        ok, img = self.cap.read()
        if not ok:
            return None
        k = self.idx
        self.idx += 1
        if self.file and not self.pace:
            if self.times is not None:
                if k >= len(self.times):
                    return None
                t = self.times[k]
            else:
                t = self.t0 + k / self.fps
        else:
            if self.pace:                                # a file standing in for a live camera
                self._wall0 = self._wall0 or time.monotonic()
                while (rem := self._wall0 + k / self.fps - time.monotonic()) > 0:
                    time.sleep(rem)
            t = self.clock()
        return Frame(k, t, _fit(img, self.w, self.h))

    def close(self) -> None:
        self.cap.release()


def open_camera(spec: str, cfg: dict, clock=time.monotonic, frames_csv=None, start=None, pace=False):
    cam = cfg["camera"]
    w, h, fps = cam["width"], cam["height"], cam["fps"]
    cap_us = cam.get("max_exposure_us")
    if spec == "auto":
        try:
            return PiCamera(w, h, fps, clock, cap_us)
        except CameraUnavailable:
            spec = "usb:0"
    if spec == "picam":
        return PiCamera(w, h, fps, clock, cap_us)
    if spec.startswith("usb:"):
        return CvCamera(int(spec[4:]), w, h, clock)
    if spec.startswith("video:"):
        return CvCamera(spec[6:], w, h, clock, frames_csv=frames_csv, start=start, pace=pace)
    raise ValueError(f"camera {spec!r}: use auto, picam, usb:N or video:PATH")
