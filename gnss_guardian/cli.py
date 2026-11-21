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
    python -m gnss_guardian.cli outputs-test [--safe-action] [--uno /dev/ttyACM0]     (LED / Uno wiring check)
    python -m gnss_guardian.cli ai-train FILE.nmea... --label "real L86, 40 min" [--first-min 40] [--out models/ai_model.json]
    python -m gnss_guardian.cli eval [--runs 10] [--out eval]      (simulator: profiles x ablation table)
    python -m gnss_guardian.cli sky-report FILE.nmea... [--almanac data/almanac.yuma]
    python -m gnss_guardian.cli ai-eval FILE.nmea... [--skip-min 40] [--model models/ai_model.json]
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
@click.option("--safe-action", is_flag=True,
              help="Demo only: drive the safe-action output (Uno relay + servo flag for a ground stand-in).")
def live(port, baud, rate, l86_constellations, out, home, config_path, clock_scale, duration_s, camera, no_camera,
         overlay_port, overlay_record, demo_inject, safe_action):
    """Read the L86 (and the camera) live: detection every fix, vision navigation every frame."""
    from gnss_guardian.io.serial_live import run_live
    if demo_inject and overlay_port is None:
        raise click.UsageError("--demo-inject needs --overlay PORT (its controls are on the guidance page)")
    cfg = load_config(config_path)
    names = [c.strip().lower() for c in l86_constellations.split(",")] if l86_constellations else None
    s = run_live(port, baud, out, cfg, rate=rate, constellations=names, clock_scale=clock_scale,
                 duration_s=duration_s, home=_home(home),
                 camera=None if no_camera else (camera or cfg["camera"]["source"]),
                 overlay_port=overlay_port, overlay_record=overlay_record, demo_inject=demo_inject,
                 safe_action=safe_action or None, echo=click.echo)
    click.echo(f"epochs {s['epochs']}, first alarms {s['first_alarm'] or '-'}")
    click.echo(f"states: {' -> '.join(s['states']) or '-'}")


@cli.command()
@click.argument("nmea_file", type=click.Path(exists=True))
@click.option("--out", default=None, type=click.Path(file_okay=False),
              help="Session folder to write (required, except for a live session folder).")
@click.option("--rerun", is_flag=True, help="Live session folder: re-run detection on its NMEA into --out "
                                            "(no camera) instead of exporting what live mode recorded.")
@click.option("--speed", default=0.0, show_default=True, type=float, help="0 = as fast as possible, N = N x real time.")
@click.option("--home", default=None, help="Home point lat,lon (default: set after the first trusted fixes).")
@click.option("--config", "config_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--video", default=None, type=click.Path(exists=True, dir_okay=False),
              help="Camera video recorded with the NMEA (tools/record_session.py): runs vision navigation too.")
@click.option("--frames", "frames_csv", default=None, type=click.Path(exists=True, dir_okay=False),
              help="Frame timestamps (default: frames.csv next to the video).")
@click.option("--video-start", default=None, help="ISO UTC time of the first frame when there is no frames.csv.")
def replay(nmea_file, out, rerun, speed, home, config_path, video, frames_csv, video_start):
    """Run detection (and vision, with --video) over a recording and print the event timeline.

    NMEA_FILE may be a recording folder: nmea.nmea or gnss.nmea, with video.avi or camera.avi and
    frames.csv used as the video when present. Writes session/NAME/dashboard.json for the dashboard.

    NMEA_FILE may also be a live session folder (live / field-demo): its dashboard.json is then written
    in that folder from what live mode recorded (state machine, vision, demo injections, overlay video);
    --rerun --out NAME re-runs detection on its NMEA instead."""
    from datetime import datetime
    from pathlib import Path

    from gnss_guardian.run import export_live_session, is_live_session, live_session_nmea
    from gnss_guardian.run import replay as run_replay
    from gnss_guardian.run import resolve_recording
    if Path(nmea_file).is_dir() and is_live_session(nmea_file):
        cfg = load_config(config_path)
        if not rerun:
            s = export_live_session(nmea_file, cfg)
            click.echo(f"live session {nmea_file}: {s['epochs']} epochs, states: {' -> '.join(map(str, s['states']))}")
            click.echo(f"dashboard: {s['dashboard']}" + (f" (video: {s['video']})" if s["video"] else " (no overlay video)"))
            return
        if out is None:
            raise click.UsageError("--rerun needs --out NAME")
        nmea_file = str(live_session_nmea(nmea_file, out))
        click.echo(f"re-running detection on {nmea_file} (no camera: live sessions keep no raw video)")
    if out is None:
        raise click.UsageError("--out is required (except for a live session folder)")
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
@click.option("--safe-action", is_flag=True, help="Also drive the Uno safe-action output (ground stand-in, demo only).")
def field_demo(out, port, http_port, camera, record, config_path, duration_s, safe_action):
    """The road demo in one command: live with camera, overlay, guidance page and demo injection."""
    from gnss_guardian.io.serial_live import run_live
    cfg = load_config(config_path)
    click.echo("GNSS Guardian field demo. Stand the rig still on the start point until the guidance page "
               "shows home set and the home keyframe stored (warm-up 60 s + home 30 s).")
    click.echo("Injected spoofs are DEMO ONLY (software, before the detector); raw/ keeps the real NMEA.")
    s = run_live(port, 9600, out, cfg, camera=camera or cfg["camera"]["source"], overlay_port=http_port,
                 overlay_record=record, demo_inject=True, duration_s=duration_s, safe_action=safe_action or None,
                 echo=click.echo)
    click.echo(f"epochs {s['epochs']}, states: {' -> '.join(s['states']) or '-'}")
    click.echo(f"session: {out}  (gnss-guardian replay {out} --out {out}_replay  opens it in the dashboard)")


@cli.command("outputs-test")
@click.option("--each-s", default=3.0, show_default=True, type=float, help="Seconds per state.")
@click.option("--safe-action", is_flag=True, help="Also drive the safe-action output (Uno or GPIO, per config).")
@click.option("--uno", default=None, help="Uno port (default: config outputs.uno.port), e.g. /dev/ttyACM0.")
@click.option("--config", "config_path", type=click.Path(exists=True, dir_okay=False))
def outputs_test(each_s, safe_action, uno, config_path):
    """Wiring check: step the LEDs (and the safe-action output) through every state, then stop to show
    the Uno watchdog dropping to SAFE."""
    import time
    from collections import deque

    from gnss_guardian.outputs import Outputs
    cfg = load_config(config_path)
    if uno:
        cfg["outputs"]["uno"]["port"] = uno
    ev = deque()
    out = Outputs(cfg, events=ev, safe_action=safe_action)
    seq = [("WARMING_UP", None), ("GNSS_OK", None), ("SUSPECT", None), ("JAMMED", "jamming"),
           ("VISION_NAV", "jamming"), ("SPOOFED", "spoofing"), ("RETURN_HOME", "spoofing"),
           ("HOME_REACHED", "spoofing"), ("GNSS_OK", None)]
    try:
        for state, cause in seq:
            t_end = time.monotonic() + each_s
            while time.monotonic() < t_end:
                out.update({"state": state, "cause": cause})
                while ev:
                    click.echo(f"  event {ev.popleft()}")
                time.sleep(0.2)
            click.echo(f"{state:<13} cause {cause or '-':<9} safe mode {out.mode}")
        click.echo(f"no updates now: STALE after {cfg['live']['no_data_s']} s; the Uno watchdog alone would go SAFE "
                   f"{cfg['outputs']['uno']['watchdog_s']} s after the heartbeat stops")
        time.sleep(cfg["live"]["no_data_s"] + 1.5)
        click.echo(f"STALE         safe mode {out.mode}")
    finally:
        out.close()
        while ev:
            click.echo(f"  event {ev.popleft()}")


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




def _read_lines(path):
    """Lines of an NMEA file, or of a recording / live session folder's NMEA."""
    from pathlib import Path

    from gnss_guardian.run import resolve_recording
    p = Path(path)
    nmea = resolve_recording(p)[0] if p.is_dir() else p
    return nmea.read_text(encoding="ascii", errors="replace").splitlines()


@cli.command("ai-train")
@click.argument("files", nargs=-1, required=True, type=click.Path(exists=True))
@click.option("--label", required=True,
              help='What it was trained on, shown with every AI output, e.g. "real L86, first 40 min of 60".')
@click.option("--out", default="models/ai_model.json", show_default=True, type=click.Path(dir_okay=False))
@click.option("--first-min", default=None, type=float, help="Only the first N minutes of each file.")
@click.option("--skip-min", default=None, type=float, help="Skip the first N minutes of each file.")
@click.option("--backend", default="auto", show_default=True, type=click.Choice(["auto", "iforest", "mahalanobis"]),
              help="auto: IsolationForest when scikit-learn is installed, plus Mahalanobis (always).")
@click.option("--config", "config_path", type=click.Path(exists=True, dir_okay=False))
def ai_train(files, label, out, first_min, skip_min, backend, config_path):
    """Train the AI second opinion on the clean epochs of real recordings (rule engine decides clean)."""
    from gnss_guardian.detect import ai
    cfg = load_config(config_path)
    X = ai.collect([_read_lines(f) for f in files], cfg, first_s=first_min * 60 if first_min else None,
                   skip_s=skip_min * 60 if skip_min else None)
    try:
        model = ai.train(X, cfg, label, sources=[str(f) for f in files], backend=backend)
    except (ValueError, ImportError) as e:
        raise click.ClickException(str(e)) from e
    ai.save(model, out)
    click.echo(f"model chosen on: {label}")
    ch = model["trained"]["checks"]
    kinds = f"runs {model['chosen']}; outlier check: " + "; ".join(f"{k} {c['detail']}" for k, c in ch.items())
    click.echo(f"{out}: {kinds}, {len(X)} clean epochs, label: {label}")


@cli.command("ai-eval")
@click.argument("files", nargs=-1, required=True, type=click.Path(exists=True))
@click.option("--model", default=None, type=click.Path(exists=True, dir_okay=False),
              help="Default: config ai.model_file.")
@click.option("--first-min", default=None, type=float)
@click.option("--skip-min", default=None, type=float, help="E.g. 40: judge only the held-out last part.")
@click.option("--backend", default=None, type=click.Choice(["auto", "iforest", "mahalanobis"]))
@click.option("--config", "config_path", type=click.Path(exists=True, dir_okay=False))
def ai_eval(files, model, first_min, skip_min, backend, config_path):
    """AI false alarms (SUSPECT epochs / episodes) and its effect on spoofing alarms, per file."""
    from gnss_guardian.detect import ai
    cfg = load_config(config_path)
    cfg["ai"]["enabled"] = True
    if model:
        cfg["ai"]["model_file"] = str(model)
    if backend:
        cfg["ai"]["backend"] = backend
    for f in files:
        try:
            r = ai.evaluate(_read_lines(f), cfg, skip_s=skip_min * 60 if skip_min else None,
                            first_s=first_min * 60 if first_min else None)
        except ValueError as e:
            raise click.ClickException(str(e)) from e
        hours = r["epochs"] / 3600
        rate = f"{r['ai_suspect_episodes'] / hours:.1f}/h" if hours else "-"
        click.echo(f"{f}: {r['epochs']} epochs ({r['epochs'] / 60:.1f} min), AI SUSPECT {r['ai_suspect_epochs']} epochs "
                   f"in {r['ai_suspect_episodes']} episodes ({rate}), score median {r['score_median']} p99 "
                   f"{r['score_p99']} max {r['score_max']}; spoofing ALARM epochs {r['spoof_alarm_epochs']} "
                   f"(rule only {r['spoof_alarm_epochs_rule_only']}) [{r['backend']}, {r['model']}]")


@cli.command("sky-report")
@click.argument("files", nargs=-1, required=True, type=click.Path(exists=True))
@click.option("--almanac", default=None, type=click.Path(exists=True, dir_okay=False),
              help="GPS YUMA almanac within 14 days of the recording (default: config detect.sky.almanac_file).")
@click.option("--config", "config_path", type=click.Path(exists=True, dir_okay=False))
def sky_report(files, almanac, config_path):
    """Sky check over recordings: GPS az/el present?, sky error vs the almanac, false SUSPECT / ALARM counts."""
    from gnss_guardian.detect import sky
    cfg = load_config(config_path)
    if almanac:
        cfg["detect"]["sky"]["almanac_file"] = str(almanac)
    for f in files:
        r = sky.report(_read_lines(f), cfg)
        d = r["detectors"]
        click.echo(f"{f}: {r['epochs']} epochs ({r['epochs'] / 60:.1f} min at 1 Hz)")
        click.echo(f"  sky check: {'ON' if r['sky_enabled'] else 'OFF'} ({r['sky_check']})")
        click.echo(f"  GPS az/el in GSV: {'yes' if r['epochs_gps_azel'] else 'NO'} ({r['epochs_gps_azel']} epochs); "
                   f"sky judged in {r['epochs_sky_judged']} epochs")
        if r["epochs_sky_judged"]:
            click.echo(f"  sky error vs almanac: median {r['sky_err_median_deg']} deg, max {r['sky_err_max_deg']} deg")
        click.echo(f"  below-horizon satellites tracked: {r['epochs_impossible']} epochs "
                   f"{' '.join(r['impossible_prns'])}".rstrip())
        for k in ("spoofing", "jamming"):
            click.echo(f"  {k}: SUSPECT {d[k]['SUSPECT']} epochs, ALARM {d[k]['ALARM']} epochs, "
                       f"{d[k]['episodes']} episodes")


@cli.command("eval")
@click.option("--runs", default=10, show_default=True, type=click.IntRange(1, 100), help="Runs per attack profile.")
@click.option("--out", default="eval", show_default=True, type=click.Path(file_okay=False))
@click.option("--profile", "profiles", multiple=True, help="Only these profiles (default: all).")
@click.option("--config", "config_path", type=click.Path(exists=True, dir_okay=False))
def eval_cmd(runs, out, profiles, config_path):
    """Simulator evaluation: every profile x {full, no-camera, no-AI, no-sky, rules-only} -> results.csv, summary.md."""
    from pathlib import Path

    from gnss_guardian import evaluate
    cfg = load_config(config_path)
    evaluate.evaluate(cfg, runs, Path(out), profiles=list(profiles) or None, echo=click.echo)
    click.echo((Path(out) / "summary.md").read_text(encoding="utf-8"))


def main():
    cli()


if __name__ == "__main__":
    main()
