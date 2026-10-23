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


class PiCamera:
    """Pi Camera Rev 1.3 (OV5647) through Picamera2/libcamera: YUV420, the Y plane is the grey image."""

    def __init__(self, width: int, height: int, fps: float, clock=time.monotonic):
        try:
            from picamera2 import Picamera2          # Raspberry Pi OS only (apt install python3-picamera2)
        except ImportError as e:
            raise CameraUnavailable("Picamera2 is only available on Raspberry Pi OS (sudo apt install "
                                    "python3-picamera2, venv with --system-site-packages); on a laptop use "
                                    "--camera usb:0 or --video FILE") from e
        self.w, self.h, self.clock = width, height, clock
        self.cam = Picamera2()
        frame_us = int(1e6 / fps)
        conf = self.cam.create_video_configuration(main={"size": (width, height), "format": "YUV420"},
                                                   controls={"FrameDurationLimits": (frame_us, frame_us)})
        self.cam.configure(conf)
        self.cam.start()
        self.idx = 0

    def read(self) -> Frame | None:
        arr = self.cam.capture_array("main")
        t = self.clock()
        self.idx += 1
        return Frame(self.idx - 1, t, np.ascontiguousarray(arr[:self.h, :self.w]))

    def close(self) -> None:
        self.cam.stop()
        self.cam.close()


def read_frames_csv(path: Path) -> list[float]:
    """Host UTC seconds per frame: column host_utc (ISO, tools/record_session.py) or t_unix (seconds,
    the standalone Pi recorders)."""
    with open(path, encoding="ascii") as f:
        rows = csv.DictReader(f)
        cols = rows.fieldnames or []
        if "host_utc" in cols:
            return [datetime.fromisoformat(r["host_utc"].replace("Z", "+00:00")).timestamp() for r in rows]
        if "t_unix" in cols:
            return [float(r["t_unix"]) for r in rows]
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
    if spec == "auto":
        try:
            return PiCamera(w, h, fps, clock)
        except CameraUnavailable:
            spec = "usb:0"
    if spec == "picam":
        return PiCamera(w, h, fps, clock)
    if spec.startswith("usb:"):
        return CvCamera(int(spec[4:]), w, h, clock)
    if spec.startswith("video:"):
        return CvCamera(spec[6:], w, h, clock, frames_csv=frames_csv, start=start, pace=pace)
    raise ValueError(f"camera {spec!r}: use auto, picam, usb:N or video:PATH")
