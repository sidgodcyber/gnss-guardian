"""gnss-guardian command-line entry point.

    python -m gnss_guardian.cli live --l86 /dev/serial0 --out session/live [--rate 1|5|10]
                                     [--l86-constellations gps,glonass,galileo] [--home lat,lon]
                                     [--camera auto|picam|usb:N|video:FILE | --no-camera]
                                     [--overlay PORT [--overlay-record] [--demo-inject]]
    python -m gnss_guardian.cli replay FILE.nmea|FOLDER --out session/NAME [--speed 0] [--home lat,lon]
                                       [--video video.avi [--frames frames.csv | --video-start ISO]]
    python -m gnss_guardian.cli status --data session/live
    python -m gnss_guardian.cli calibrate --video walk/video.avi --distance 10 [--height 1.0] [--dry-run]
    python -m gnss_guardian.cli calibrate --live --distance 5 [--camera picam]     (on the Pi, on the road)
    python -m gnss_guardian.cli field-demo --out session/NAME [--l86 /dev/serial0] [--port 8080]
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


L86_RATES_HZ = (1, 5, 10)


def _rate(ctx, param, value):
    """--rate as a number (1, "1", 5.0): a string Choice rejected the int default on older Click."""
    if value not in L86_RATES_HZ:
        raise click.BadParameter(f"{value:g} Hz is not supported; use 1, 5 or 10")
    return int(value)


@click.group()
def cli():
    """GNSS jamming/spoofing detection (Phase 1 of gnss-guardian)."""


@cli.command()
@click.option("--l86", "port", required=True,
              help="Serial port (/dev/serial0, COM3) or pyserial URL (socket://127.0.0.1:5000).")
@click.option("--baud", default=9600, show_default=True, type=int)
@click.option("--rate", default=1, show_default=True, type=float, callback=_rate, metavar="1|5|10",
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
@click.option("--overlay", "overlay_port", default=None, type=click.IntRange(0, 65535), metavar="PORT",
              help="Serve the camera overlay (MJPEG at /), the guidance page (/guide) and /live.json on this port.")
@click.option("--overlay-record", is_flag=True, help="Also save the overlay to session/NAME/overlay.mp4.")
@click.option("--demo-inject", is_flag=True,
              help="Demo only: inject a software spoof (jump / drift) into the NMEA before the detector, from "
                   "buttons on the guidance page. Needs --overlay.")
def live(port, baud, rate, l86_constellations, out, home, config_path, clock_scale, duration_s, camera, no_camera,
         overlay_port, overlay_record, demo_inject):
    """Read the L86 (and the camera) live: detection every fix, vision navigation every frame."""
    from gnss_guardian.io.serial_live import run_live
    if demo_inject and overlay_port is None:
        raise click.UsageError("--demo-inject needs --overlay PORT (its controls are on the guidance page)")
    cfg = load_config(config_path)
    names = [c.strip().lower() for c in l86_constellations.split(",")] if l86_constellations else None
    s = run_live(port, baud, out, cfg, rate=rate, constellations=names, clock_scale=clock_scale,
                 duration_s=duration_s, home=_home(home),
                 camera=None if no_camera else (camera or cfg["camera"]["source"]),
                 overlay_port=overlay_port, overlay_record=overlay_record, demo_inject=demo_inject, echo=click.echo)
    click.echo(f"epochs {s['epochs']}, first alarms {s['first_alarm'] or '-'}")
    click.echo(f"states: {' -> '.join(s['states']) or '-'}")


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
    click.echo(f"states: {' -> '.join(s['states']) or '-'}")
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
@click.option("--video", default=None, type=click.Path(exists=True, dir_okay=False),
              help="Video of a straight walk of known length (tools/record_session.py).")
@click.option("--live", "live_cam", is_flag=True,
              help="Use the camera now (on the Pi): Enter at the start mark, push the rig, Enter at the end mark.")
@click.option("--camera", default=None, help="With --live: auto | picam | usb:N (default: config camera.source).")
@click.option("--distance", required=True, type=float,
              help="True length of the walk in metres (tape-measured; 5 or 10 m pushed on the road).")
@click.option("--height", default=None, type=float, help="Camera height above ground in m (default: config camera.height_m).")
@click.option("--dry-run", is_flag=True, help="Print the fit, do not write config/calibration.yaml.")
@click.option("--config", "config_path", type=click.Path(exists=True, dir_okay=False))
def calibrate(video, live_cam, camera, distance, height, dry_run, config_path):
    """Fit the camera focal length (metres per pixel at the working height) from a measured walk."""
    from gnss_guardian.vision.calibrate import calibrate as fit
    from gnss_guardian.vision.calibrate import write_calibration
    if bool(video) == bool(live_cam):
        raise click.UsageError("give either --video FILE or --live")
    cfg = load_config(config_path)
    if live_cam:
        r = _calibrate_live(cfg, camera or cfg["camera"]["source"], distance, height)
    else:
        r = fit(video, distance, cfg, height)
    click.echo(f"{r['true_m']} m walk measured {r['measured_m']} m (path {r['path_m']} m, straightness "
               f"{r['straightness']}); focal {r['previous_focal_px']} -> {r['focal_px']} px "
               f"(HFOV equivalent {r['hfov_deg_equivalent']} deg) at {r['height_m']} m; "
               f"{r['frames']} frames, {r['low_quality_frames']} low-quality")
    if r["straightness"] and r["straightness"] < 0.95:
        click.echo("warning: the walk was not straight (straightness < 0.95); repeat along a taped line")
    if not dry_run:
        click.echo(f"wrote {write_calibration(r, cfg)}")


def _calibrate_live(cfg, spec, distance, height):
    import threading

    from gnss_guardian.vision.calibrate import calibrate_frames
    from gnss_guardian.vision.camera import CameraUnavailable, open_camera
    try:
        src = open_camera(spec, cfg)
    except (CameraUnavailable, ValueError) as e:
        raise click.ClickException(f"camera {spec}: {e}") from e
    try:
        click.echo(f"camera {spec} open, {cfg['camera']['height_m'] if height is None else height} m up. Put the rig on "
                   f"the start mark and press Enter.")
        input()
        done = threading.Event()

        def wait_enter():
            try:
                input()
                done.set()
            except EOFError:                 # no terminal: run until the source ends
                pass
        threading.Thread(target=wait_enter, daemon=True).start()
        click.echo(f"Push the rig straight {distance:g} m to the end mark, stop, then press Enter.")

        def frames():
            while not done.is_set():
                fr = src.read()
                if fr is None:
                    return
                yield fr
        return calibrate_frames(frames(), distance, cfg, height, f"live camera {spec}",
                                progress=lambda m, n: click.echo(f"\r  {m:5.2f} m measured ({n} frames)", nl=False))
    finally:
        click.echo("")
        src.close()


@cli.command("field-demo")
@click.option("--out", required=True, type=click.Path(file_okay=False), help="Session folder, e.g. session/road1.")
@click.option("--l86", "port", default="/dev/serial0", show_default=True)
@click.option("--port", "http_port", default=8080, show_default=True, type=click.IntRange(1, 65535),
              help="Overlay stream, guidance page and live.json.")
@click.option("--camera", default=None, help="auto | picam | usb:N | video:FILE (default: config camera.source).")
@click.option("--record/--no-record", default=True, show_default=True, help="Save overlay.mp4 in the session folder.")
@click.option("--config", "config_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--duration-s", default=None, type=float, help="Stop after this many seconds (default: Ctrl-C).")
def field_demo(out, port, http_port, camera, record, config_path, duration_s):
    """The road demo in one command: live with camera, overlay, guidance page and demo injection."""
    from gnss_guardian.io.serial_live import run_live
    cfg = load_config(config_path)
    click.echo("GNSS Guardian field demo. Stand the rig still on the start point until the guidance page "
               "shows home set and the home keyframe stored (warm-up 60 s + home 30 s).")
    click.echo("Injected spoofs are DEMO ONLY (software, before the detector); raw/ keeps the real NMEA.")
    s = run_live(port, 9600, out, cfg, camera=camera or cfg["camera"]["source"], overlay_port=http_port,
                 overlay_record=record, demo_inject=True, duration_s=duration_s, echo=click.echo)
    click.echo(f"epochs {s['epochs']}, states: {' -> '.join(s['states']) or '-'}")
    click.echo(f"session: {out}  (gnss-guardian replay {out} --out {out}_replay  opens it in the dashboard)")


@cli.command("vision-bench")
@click.option("--video", default=None, type=click.Path(exists=True, dir_okay=False))
@click.option("--frames", "n", default=300, show_default=True, type=int)
@click.option("--scale", default=None, type=float, help="Override vision.process_scale (e.g. 0.5).")
@click.option("--threads", default=None, type=int, help="OpenCV threads (default: all cores).")
@click.option("--overlay", is_flag=True, help="Run twice: without, then with the live overlay thread rendering.")
def vision_bench(video, n, scale, threads, overlay):
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
    for ov in ((False, True) if overlay else (False,)):
        r = bench.run(cfg, frames, n, overlay=ov)
        click.echo(("with overlay    " if ov else "without overlay " if overlay else "") +
                   " | ".join(f"{k} {v}" for k, v in r.items()))


def main():
    cli()


if __name__ == "__main__":
    main()
