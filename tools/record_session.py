#!/usr/bin/env python3
"""Record a field session on the Pi: Pi Camera video + L86 NMEA, on one host clock. Standalone
(no repo imports): needs python3-picamera2 (or OpenCV for --camera usb:N / video:FILE), python3-opencv,
and python3-serial for NMEA.

    python3 record_session.py --out walk01 [--duration-min 5] [--camera picam] [--port /dev/serial0]
                              [--fps 30] [--size 640x480] [--no-nmea]

writes
  walk01/video.avi    MJPG, grey, 640x480 (about 1 MB/s at 30 fps: 5 min ~ 300 MB)
  walk01/frames.csv   idx,host_utc  (host UTC when each frame arrived)
  walk01/nmea.nmea    host-prefixed NMEA (the tools/record_nmea.py format), if --port is readable

Replay both on the laptop:
  gnss-guardian replay walk01/nmea.nmea --video walk01/video.avi --out session/walk01
Calibrate from a measured straight walk:
  gnss-guardian calibrate --video walk01/video.avi --distance 10

Camera: Pi Camera Rev 1.3 (OV5647) pointing straight down at the rig's working height. A short
exposure is requested to limit motion blur while walking (needs daylight).
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2


def stamp(t: datetime) -> str:
    return f"{t:%Y-%m-%dT%H:%M:%S}.{t.microsecond // 1000:03d}Z"


class PiCam:
    def __init__(self, w, h, fps):
        from picamera2 import Picamera2
        self.w, self.h = w, h
        self.cam = Picamera2()
        us = int(1e6 / fps)
        controls = {"FrameDurationLimits": (us, us)}
        try:
            from libcamera import controls as lc
            controls["AeExposureMode"] = lc.AeExposureModeEnum.Short      # less motion blur
        except Exception:
            pass
        self.cam.configure(self.cam.create_video_configuration(main={"size": (w, h), "format": "YUV420"},
                                                               controls=controls))
        self.cam.start()

    def read(self):
        return self.cam.capture_array("main")[:self.h, :self.w]

    def close(self):
        self.cam.stop()
        self.cam.close()


class CvCam:
    def __init__(self, src, w, h, fps):
        self.cap = cv2.VideoCapture(src)
        if not self.cap.isOpened():
            raise RuntimeError(f"cannot open {src!r}")
        self.w, self.h, self.file = w, h, isinstance(src, str)
        self.period = 1.0 / (self.cap.get(cv2.CAP_PROP_FPS) or fps) if self.file else 0.0
        self._next = time.monotonic()

    def read(self):
        if self.file:                                    # a file stands in for a camera: real-time pace
            while (rem := self._next - time.monotonic()) > 0:
                time.sleep(rem)
            self._next += self.period
        ok, img = self.cap.read()
        if not ok:
            return None
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        return cv2.resize(g, (self.w, self.h), interpolation=cv2.INTER_AREA) if g.shape[:2] != (self.h, self.w) else g

    def close(self):
        self.cap.release()


def nmea_thread(port, baud, path, stop, stats):
    try:
        import serial
    except ImportError:
        print("pyserial missing (sudo apt install python3-serial): no NMEA", flush=True)
        return
    ser, pending = None, b""
    with open(path, "a", encoding="ascii", errors="replace", newline="\n") as out:
        while not stop.is_set():
            if ser is None:
                try:
                    ser = serial.serial_for_url(port, baudrate=baud, timeout=0.2)
                except (serial.SerialException, OSError, ValueError) as e:
                    stats["nmea_error"] = str(e)
                    time.sleep(2)
                    continue
            try:
                chunk = ser.read(ser.in_waiting or 1)
            except (serial.SerialException, OSError):
                ser.close()
                ser = None
                continue
            if chunk:
                now = datetime.now(timezone.utc)
                pending += chunk
                *lines, pending = pending.split(b"\n")
                for bl in lines:
                    line = bl.decode("ascii", errors="replace").strip()
                    if line:
                        out.write(f"{stamp(now)},{line}\n")
                        stats["nmea_lines"] += 1
                        if line[3:6] == "RMC":
                            f = line.split(",")
                            stats["fix"] = f[2] if len(f) > 2 else "-"
                out.flush()
    if ser is not None:
        ser.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="output folder")
    ap.add_argument("--duration-min", type=float, default=5.0)
    ap.add_argument("--camera", default="picam", help="picam | usb:N | video:FILE")
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--size", default="640x480")
    ap.add_argument("--port", default="/dev/serial0")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--no-nmea", action="store_true")
    a = ap.parse_args(argv)
    w, h = (int(x) for x in a.size.split("x"))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if a.camera == "picam":
        cam = PiCam(w, h, a.fps)
    elif a.camera.startswith("usb:"):
        cam = CvCam(int(a.camera[4:]), w, h, a.fps)
    elif a.camera.startswith("video:"):
        cam = CvCam(a.camera[6:], w, h, a.fps)
    else:
        print(f"unknown camera {a.camera!r}", file=sys.stderr)
        return 2
    vw = cv2.VideoWriter(str(out / "video.avi"), cv2.VideoWriter_fourcc(*"MJPG"), a.fps, (w, h), isColor=False)
    stop = threading.Event()
    stats = {"nmea_lines": 0, "fix": "-", "nmea_error": None}
    th = None
    if not a.no_nmea:
        th = threading.Thread(target=nmea_thread, args=(a.port, a.baud, out / "nmea.nmea", stop, stats), daemon=True)
        th.start()
    print(f"recording {a.camera} {w}x{h}@{a.fps:g} -> {out} for {a.duration_min:g} min (Ctrl-C stops)", flush=True)
    n, t0, t_status = 0, time.monotonic(), time.monotonic()
    try:
        with open(out / "frames.csv", "a", encoding="ascii", newline="\n") as fc:
            if fc.tell() == 0:
                fc.write("idx,host_utc\n")
            while time.monotonic() - t0 < a.duration_min * 60:
                img = cam.read()
                if img is None:
                    break
                fc.write(f"{n},{stamp(datetime.now(timezone.utc))}\n")
                vw.write(img)
                n += 1
                if time.monotonic() - t_status >= 10:
                    el = time.monotonic() - t0
                    print(f"{stamp(datetime.now(timezone.utc))} | {el / 60:4.1f} min | frames {n} ({n / el:.1f} fps) | "
                          f"mean grey {img.mean():.0f} | NMEA lines {stats['nmea_lines']} RMC {stats['fix']}"
                          + (f" | NMEA error: {stats['nmea_error']}" if stats["nmea_error"] and not stats["nmea_lines"] else ""),
                          flush=True)
                    t_status = time.monotonic()
    except KeyboardInterrupt:
        print("stopped by user", flush=True)
    finally:
        stop.set()
        cam.close()
        vw.release()
        if th:
            th.join(timeout=2)
    print(f"done: {n} frames, {stats['nmea_lines']} NMEA lines in {out}", flush=True)
    return 0 if n else 1


if __name__ == "__main__":
    sys.exit(main())
