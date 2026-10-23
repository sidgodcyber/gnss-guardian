"""gnss-guardian command-line entry point.

    python -m gnss_guardian.cli live --l86 /dev/serial0 --out session/live [--rate 1|5|10]
                                     [--l86-constellations gps,glonass,galileo] [--home lat,lon]
                                     [--camera auto|picam|usb:N|video:FILE | --no-camera]
    python -m gnss_guardian.cli replay FILE.nmea|FOLDER --out session/NAME [--speed 0] [--home lat,lon]
                                       [--video video.avi [--frames frames.csv | --video-start ISO]]
    python -m gnss_guardian.cli status --data session/live
    python -m gnss_guardian.cli calibrate --video walk/video.avi --distance 10 [--height 1.0] [--dry-run]
    python -m gnss_guardian.cli vision-bench [--video FILE] [--frames 300] [--scale 0.5] [--threads N]
"""
from __future__ import annotations

import sys

import click

from gnss_guardian.config import load_config
from gnss_guardian.home import parse_home


def _home(value):
    try:
        return parse_home(value)
    except ValueError as e:
        raise click.BadParameter(str(e)) from e


@click.group()
def cli():
    """GNSS jamming/spoofing detection (Phase 1 of gnss-guardian)."""


@cli.command()
@click.option("--l86", "port", required=True,
              help="Serial port (/dev/serial0, COM3) or pyserial URL (socket://127.0.0.1:5000).")
@click.option("--baud", default=9600, show_default=True, type=int)
@click.option("--rate", default=1, show_default=True, type=click.Choice(["1", "5", "10"]),
              help="L86 fix rate in Hz (5/10 switch the module to 115200 baud).")
@click.option("--l86-constellations", default=None,
              help="Send PMTK353, e.g. gps,glonass,galileo (verified by GAGSV appearing).")
@click.option("--out", default="session/live", show_default=True, type=click.Path(file_okay=False))
@click.option("--home", default=None, help="Home point lat,lon (default: set after the first trusted fixes).")
@click.option("--config", "config_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--clock-scale", default=1.0, show_default=True, type=float,
              help="Only with a fast simulator: simulated seconds per wall second.")
@click.option("--duration-s", default=None, type=float, help="Stop after this many wall seconds.")
@click.option("--camera", default=None, help="auto | picam | usb:N | video:FILE (default: config camera.source).")
@click.option("--no-camera", is_flag=True, help="GNSS detection only.")
def live(port, baud, rate, l86_constellations, out, home, config_path, clock_scale, duration_s, camera, no_camera):
    """Read the L86 (and the camera) live: detection every fix, vision navigation every frame."""
    from gnss_guardian.io.serial_live import run_live
    cfg = load_config(config_path)
    names = [c.strip().lower() for c in l86_constellations.split(",")] if l86_constellations else None
    s = run_live(port, baud, out, cfg, rate=int(rate), constellations=names, clock_scale=clock_scale,
                 duration_s=duration_s, home=_home(home),
                 camera=None if no_camera else (camera or cfg["camera"]["source"]))
    click.echo(f"epochs {s['epochs']}, first alarms {s['first_alarm'] or '-'}")


@cli.command()
@click.argument("nmea_file", type=click.Path(exists=True))
@click.option("--out", required=True, type=click.Path(file_okay=False))
@click.option("--speed", default=0.0, show_default=True, type=float, help="0 = as fast as possible, N = N x real time.")
@click.option("--home", default=None, help="Home point lat,lon (default: set after the first trusted fixes).")
@click.option("--config", "config_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--video", default=None, type=click.Path(exists=True, dir_okay=False),
              help="Camera video recorded with the NMEA (tools/record_session.py): runs vision navigation too.")
@click.option("--frames", "frames_csv", default=None, type=click.Path(exists=True, dir_okay=False),
              help="Frame timestamps (default: frames.csv next to the video).")
@click.option("--video-start", default=None, help="ISO UTC time of the first frame when there is no frames.csv.")
def replay(nmea_file, out, speed, home, config_path, video, frames_csv, video_start):
    """Run detection (and vision, with --video) over a recording and print the event timeline.

    NMEA_FILE may be a recording folder: nmea.nmea or gnss.nmea, with video.avi or camera.avi and
    frames.csv used as the video when present. Writes session/NAME/dashboard.json for the dashboard."""
    from datetime import datetime
    from pathlib import Path

    from gnss_guardian.run import replay as run_replay
    from gnss_guardian.run import resolve_recording
    if Path(nmea_file).is_dir():
        try:
            nmea_file, found_video, found_frames = resolve_recording(nmea_file)
        except FileNotFoundError as e:
            raise click.BadParameter(str(e)) from e
        if video is None and found_video is not None:
            video, frames_csv = found_video, frames_csv or found_frames
        click.echo(f"recording: {nmea_file}" + (f" + {video}" if video else ""))
    cfg = load_config(config_path)
    start = datetime.fromisoformat(video_start.replace("Z", "+00:00")) if video_start else None
    s = run_replay(nmea_file, cfg, out, speed=speed, home=_home(home), echo=click.echo, video=video,
                   frames_csv=frames_csv, video_start=start)
    click.echo(f"epochs {s['epochs']} | jamming ALARM {s['jamming_alarm_epochs']} / SUSPECT "
               f"{s['jamming_suspect_epochs']} | spoofing ALARM {s['spoofing_alarm_epochs']} / SUSPECT "
               f"{s['spoofing_suspect_epochs']} | first alarms {s['first_alarm'] or '-'}")
    if video:
        click.echo(f"vision: {s['frames']} frames, {s['keyframes']} keyframes")
    click.echo(f"session: {out} (dashboard: {s['dashboard']})")


@cli.command()
@click.option("--data", "folder", default="session/live", show_default=True, type=click.Path(file_okay=False))
@click.option("--every", default=1.0, show_default=True, type=float)
@click.option("--once", is_flag=True)
def status(folder, every, once):
    """One status line per second from the session's latest.json."""
    from gnss_guardian import status as st
    cfg = load_config()
    if once:
        line, stale = st.status_line(folder, cfg["display_tz"])
        click.echo(line)
        sys.exit(1 if stale else 0)
    try:
        st.watch(folder, every, echo=click.echo, tz=cfg["display_tz"])
    except KeyboardInterrupt:
        pass


@cli.command()
@click.option("--video", required=True, type=click.Path(exists=True, dir_okay=False),
              help="Video of a straight walk of known length (tools/record_session.py).")
@click.option("--distance", required=True, type=float, help="True length of the walk in metres (tape-measured).")
@click.option("--height", default=None, type=float, help="Camera height above ground in m (default: config camera.height_m).")
@click.option("--dry-run", is_flag=True, help="Print the fit, do not write config/calibration.yaml.")
@click.option("--config", "config_path", type=click.Path(exists=True, dir_okay=False))
def calibrate(video, distance, height, dry_run, config_path):
    """Fit the camera focal length (metres per pixel at the working height) from a measured walk."""
    from gnss_guardian.vision.calibrate import calibrate as fit
    from gnss_guardian.vision.calibrate import write_calibration
    cfg = load_config(config_path)
    r = fit(video, distance, cfg, height)
    click.echo(f"{r['true_m']} m walk measured {r['measured_m']} m (path {r['path_m']} m, straightness "
               f"{r['straightness']}); focal {r['previous_focal_px']} -> {r['focal_px']} px "
               f"(HFOV equivalent {r['hfov_deg_equivalent']} deg) at {r['height_m']} m; "
               f"{r['frames']} frames, {r['low_quality_frames']} low-quality")
    if r["straightness"] and r["straightness"] < 0.95:
        click.echo("warning: the walk was not straight (straightness < 0.95); repeat along a taped line")
    if not dry_run:
        click.echo(f"wrote {write_calibration(r, cfg)}")


@cli.command("vision-bench")
@click.option("--video", default=None, type=click.Path(exists=True, dir_okay=False))
@click.option("--frames", "n", default=300, show_default=True, type=int)
@click.option("--scale", default=None, type=float, help="Override vision.process_scale (e.g. 0.5).")
@click.option("--threads", default=None, type=int, help="OpenCV threads (default: all cores).")
def vision_bench(video, n, scale, threads):
    """Measure the vision frame rate on this machine (run it on the laptop and on the Pi)."""
    import cv2

    from gnss_guardian.vision import bench
    cfg = load_config()
    if scale is not None:
        cfg["vision"]["process_scale"] = scale
    if threads is not None:
        cv2.setNumThreads(threads)
    frames = None
    if video:
        from gnss_guardian.vision.camera import _fit
        cap = cv2.VideoCapture(video)
        frames = []
        while len(frames) < n:
            ok, img = cap.read()
            if not ok:
                break
            frames.append(_fit(img, cfg["camera"]["width"], cfg["camera"]["height"]))
    r = bench.run(cfg, frames, n)
    click.echo(" | ".join(f"{k} {v}" for k, v in r.items()))


def main():
    cli()


if __name__ == "__main__":
    main()
