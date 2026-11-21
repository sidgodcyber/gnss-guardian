"""Live L86 over serial: read NMEA, emit an epoch the moment each burst ends, run detection.

Reused from IonWatch live mode: reconnect after unplug/open failure, reopen after a silent port
(a GPIO UART does not error when the L86 loses power, it goes quiet: event no_serial_data),
partial-line and garbage handling, raw NMEA log with host-time prefix, and the PMTK rate setup
(PMTK251 baud, PMTK314 sentences, PMTK220 fix interval, measured rate with fallback to 1 Hz).
New: PMTK353 to enable Galileo (verified by GAGSV appearing), PGCMD,33,1 for periodic antenna
status, epoch-level output, no_data events, optional camera thread (vision navigation), live.json
every epoch, and with --overlay PORT a display server (camera overlay stream, guidance page) and
with --demo-inject software spoof injection into the NMEA stream before the detector.

    gnss-guardian live --l86 /dev/serial0 [--rate 1|5|10] [--l86-constellations gps,glonass,galileo]
                       [--camera auto|picam|usb:N|video:FILE | --no-camera] --out session/NAME [--home lat,lon]
                       [--overlay PORT [--overlay-record] [--demo-inject]]

Demo injection: raw/ keeps the NMEA exactly as received; detector_input/ the stream the detector saw
(raw plus the injected offsets); every start/stop is a demo_inject event.

Time: mono = time.monotonic() x clock_scale (clock_scale > 1 only to pair with a fast simulator).
No network is used: the port is a device or the pyserial URL the user passes.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from gnss_guardian.livehub import LiveHub
from gnss_guardian.run import Pipeline
from gnss_guardian.session import SessionWriter, utc_now

log = logging.getLogger("gnss_guardian.live")

# PMTK314 field order: GLL, RMC, VTG, GGA, GSA, GSV, GRS, GST, (10 reserved), ZDA, MCHN.
PMTK314_RMC_GGA_GSA_GSV = "PMTK314,0,1,0,1,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0"
HIGH_BAUD = 115200
CONSTELLATIONS = ("gps", "glonass", "galileo", "galileo_full", "beidou")


def pmtk(body: str) -> bytes:
    x = 0
    for ch in body:
        x ^= ord(ch)
    return f"${body}*{x:02X}\r\n".encode("ascii")


def pmtk353(names: list[str]) -> str:
    """PMTK353,GPS,GLONASS,GALILEO,GALILEO_FULL,BEIDOU from names like ['gps','glonass','galileo']."""
    bad = set(names) - set(CONSTELLATIONS)
    if bad:
        raise ValueError(f"unknown constellations {sorted(bad)}; use {', '.join(CONSTELLATIONS)}")
    return "PMTK353," + ",".join("1" if c in names else "0" for c in CONSTELLATIONS)


def setup_logging(out_dir: Path, name: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("gnss_guardian")
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        if getattr(h, "_guardian", False):
            root.removeHandler(h)
            h.close()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for h in (logging.FileHandler(out_dir / name, encoding="utf-8"), logging.StreamHandler()):
        h.setFormatter(fmt)
        h._guardian = True
        root.addHandler(h)


class RawNmeaLog:
    """Raw NMEA with a host-time prefix, one file per host UTC day: raw/nmea_YYYY-MM-DD.nmea
    (the format `gnss-guardian replay` and tools/record_nmea.py use)."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self._day, self._f = None, None

    def write(self, line: str, host: datetime) -> None:
        if not line:
            return
        day = f"{host:%Y-%m-%d}"
        if day != self._day:
            self.close()
            self._day = day
            self._f = open(self.folder / f"nmea_{day}.nmea", "a", encoding="ascii", errors="replace")
        self._f.write(f"{host:%Y-%m-%dT%H:%M:%S}.{host.microsecond // 1000:03d}Z,{line}\n")

    def flush(self):
        if self._f:
            self._f.flush()

    def close(self):
        if self._f:
            self._f.close()
            self._f = None


class L86RateSetup:
    """Configure the L86 output rate (and constellations) on every (re)open; the module forgets its
    settings when it loses power (no backup battery).

    rate 1: leave the module at 9600 baud, 1 Hz. rate 5/10: PMTK251 (115200), reopen at 115200,
    PMTK314 (RMC, GGA, GSA, GSV only) and PMTK220. The epoch rate is then measured over
    ``measure_s`` data seconds; below 80 % of the request the module goes back to 1 Hz at the start
    baud (event rate_fallback).
    """

    def __init__(self, rate: int, baud: int, measure_s: float = 30.0, constellations: str | None = None,
                 antenna_status: bool = False):
        self.requested, self.baud, self.measure_s = rate, baud, measure_s
        self.constellations = constellations
        self.antenna_status = antenna_status
        self.fallen_back = False
        self.measured: float | None = None
        self._epochs: set = set()
        self._t0 = None

    @property
    def high(self) -> bool:
        return self.requested > 1 and not self.fallen_back

    def open(self, serial_mod, port: str, timeout: float):
        ser = serial_mod.serial_for_url(port, baudrate=self.baud, timeout=timeout)
        if self.high:
            ser.write(pmtk(f"PMTK251,{HIGH_BAUD}"))
            ser.flush()
            time.sleep(0.1)
            ser.close()
            ser = serial_mod.serial_for_url(port, baudrate=HIGH_BAUD, timeout=timeout)
            ser.write(pmtk(PMTK314_RMC_GGA_GSA_GSV))
            ser.write(pmtk(f"PMTK220,{int(1000 / self.requested)}"))
            self._epochs, self._t0, self.measured = set(), None, None     # re-measure after every open
            log.info("L86 configured: %d baud, RMC/GGA/GSA/GSV only, %d Hz requested", HIGH_BAUD, self.requested)
        elif self.fallen_back:                           # module may have kept 5/10 Hz settings
            ser.write(pmtk("PMTK220,1000"))
        if self.constellations:
            ser.write(pmtk(self.constellations))
            log.info("L86 constellations: %s", self.constellations)
        if self.antenna_status:
            ser.write(pmtk("PGCMD,33,1"))               # periodic PGTOP,11,x antenna status
        ser.flush()
        return ser

    def observe(self, t_gnss) -> str | None:
        """Feed each epoch's GNSS time; 'ok' or 'fallback' once the measurement window is complete."""
        if not self.high or self.measured is not None or t_gnss is None:
            return None
        ts = t_gnss.timestamp()
        if self._t0 is None:
            self._t0 = float(int(ts) + 1)                # start on a whole second
        if ts >= self._t0:
            self._epochs.add(round(ts, 2))
        if ts - self._t0 >= self.measure_s:
            self.measured = round(len(self._epochs) / (ts - self._t0), 2)
            return "fallback" if self.measured < 0.8 * self.requested else "ok"
        return None

    def fallback(self, ser, serial_mod, port: str, timeout: float):
        log.warning("L86 epoch rate %.2f Hz < 80 %% of the requested %d Hz: back to 1 Hz at %d baud",
                    self.measured, self.requested, self.baud)
        ser.write(pmtk("PMTK220,1000"))
        ser.write(pmtk(f"PMTK251,{self.baud}"))
        ser.flush()
        time.sleep(0.1)
        ser.close()
        self.fallen_back = True
        return serial_mod.serial_for_url(port, baudrate=self.baud, timeout=timeout)


class CameraThread(threading.Thread):
    """Feeds camera frames to VisionNav until stopped or the source ends; counts the frame rate."""

    def __init__(self, source, nav, writer):
        super().__init__(daemon=True, name="camera")
        self.source, self.nav, self.writer = source, nav, writer
        self.stop = threading.Event()
        self.frames = 0
        self.fps: float | None = None
        self.target_fps: float | None = getattr(source, "fps", None)     # the source's own frame rate
        self._exp_mode = None
        self.error: str | None = None

    def run(self):
        t0, n0 = time.monotonic(), 0
        try:
            while not self.stop.is_set():
                fr = self.source.read()
                if fr is None:
                    break
                self.nav.on_frame(fr.t, fr.gray)
                self.frames += 1
                exp = getattr(self.source, "exposure", None)
                if exp and exp.get("mode") != self._exp_mode:       # capped <-> auto: say so
                    self._exp_mode = exp["mode"]
                    self.writer.event("camera_exposure", **exp)
                    log.info("camera exposure %s: %s us, gain %s (cap %s us)", exp["mode"], exp["exposure_us"],
                             exp["gain"], exp["max_exposure_us"])
                if time.monotonic() - t0 >= 10:
                    self.fps = round((self.frames - n0) / (time.monotonic() - t0), 1)
                    self.writer.event("vision_rate", fps=self.fps, frames=self.frames, **(exp or {}))
                    t0, n0 = time.monotonic(), self.frames
        except Exception as e:                           # a camera fault must not stop GNSS detection
            self.error = repr(e)
            log.exception("camera thread stopped")
        finally:
            self.source.close()


def start_camera(spec: str, cfg: dict, out_dir: Path, writer, clock):
    """(VisionNav, CameraThread), or (None, None) when the camera cannot be opened: live mode then
    runs GNSS-only and says so."""
    from gnss_guardian.vision.camera import CameraUnavailable, open_camera
    from gnss_guardian.vision.nav import VisionNav
    try:
        src = open_camera(spec, cfg, clock=clock, pace=spec.startswith("video:"))
    except (CameraUnavailable, ValueError) as e:
        log.warning("camera %s unavailable (%s): running GNSS-only, no vision navigation", spec, e)
        writer.event("camera_unavailable", camera=spec, error=str(e))
        return None, None
    nav = VisionNav(cfg, keyframe_dir=out_dir / "keyframes")
    th = CameraThread(src, nav, writer)
    th.start()
    writer.event("camera_open", camera=spec)
    log.info("camera %s open", spec)
    return nav, th


def stop_on_sigterm() -> None:
    """systemd / kill send SIGTERM: stop like Ctrl-C, so the files are closed (overlay.mp4 is unreadable
    without its trailer). Only possible from the main thread."""
    import signal

    def raise_interrupt(signum, frame):
        raise KeyboardInterrupt
    if threading.current_thread() is threading.main_thread() and hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, raise_interrupt)


def start_display(cfg: dict, hub: LiveHub, nav, writer, out_dir: Path, port: int | None, record: bool,
                  cam_thread, on_inject, echo, clock=time.monotonic):
    """(server, overlay thread); either is None when not wanted or not possible. Display only: a
    failure here is logged and live mode carries on."""
    from gnss_guardian.overlay import OverlayThread
    server = None
    if port is not None:
        from gnss_guardian.web.server import LiveServer
        server = LiveServer(hub, port, on_inject=on_inject)
        if server.start():
            urls = server.urls()
            writer.event("display_server", port=server.port, **urls)
            echo(f"overlay stream  {urls['overlay']}")
            echo(f"guidance page   {urls['guide']}")
            echo(f"live.json       {urls['live_json']}")
        else:
            writer.event("overlay_unavailable", port=port, error=server.error)
            server = None
    ov = None
    if server is not None or record:
        ov = OverlayThread(cfg, hub, nav, record=out_dir / "overlay.mp4" if record else None,
                           camera_fps=lambda: cam_thread.fps if cam_thread else None,
                           camera_target_fps=cam_thread.target_fps if cam_thread else None, clock=clock)
        ov.start()
    return server, ov


def run_live(port: str, baud: int, out_dir, cfg: dict, rate: int = 1, constellations: list[str] | None = None,
             clock_scale: float = 1.0, duration_s: float | None = None, home=None, echo=print,
             camera: str | None = None, overlay_port: int | None = None, overlay_record: bool = False,
             demo_inject: bool = False) -> dict:
    """Read NMEA from ``port`` (/dev/serial0, COM3, or a pyserial URL such as socket://127.0.0.1:5000)
    until stopped or ``duration_s`` wall seconds. Returns the pipeline summary."""
    import serial

    from gnss_guardian.inject import DemoInjector

    out_dir = Path(out_dir)
    setup_logging(out_dir, "live.log")
    lv = cfg["live"]
    cmd353 = pmtk353(constellations) if constellations else None
    writer = SessionWriter(out_dir, {"mode": "live", "port": port, "baud": baud, "node_id": cfg["node_id"],
                                     "rate_requested": rate, "constellations": cmd353}, append=True)
    nav = cam_thread = None
    if camera:
        nav, cam_thread = start_camera(camera, cfg, out_dir, writer, lambda: time.monotonic() * clock_scale)
    pipe = Pipeline(cfg, writer, home, echo=lambda s: log.info(s), vision=nav)
    raw = RawNmeaLog(out_dir / "raw")
    injector = DemoInjector(clock=lambda: time.monotonic() * clock_scale) if demo_inject else None
    det_raw = RawNmeaLog(out_dir / "detector_input") if injector else None
    pending_events: deque = deque()                 # from the display server's thread, logged by this one

    def on_inject(kind, result):
        pending_events.append(("demo_inject", {"action": kind, "at_utc": utc_now(),
                                               "mono": round(time.monotonic() * clock_scale, 3), **(result or {})}))

    hub = LiveHub(cfg, out_dir, injector, vision_fps=lambda: cam_thread.fps if cam_thread else None)
    server, overlay = start_display(cfg, hub, nav, writer, out_dir, overlay_port, overlay_record, cam_thread,
                                    on_inject, echo, clock=lambda: time.monotonic() * clock_scale)
    if injector:
        writer.event("demo_inject_enabled", note="software injection before the detector; raw/ is unmodified")
    setup = L86RateSetup(rate, baud, lv["rate_measure_s"], cmd353, cfg["l86"]["antenna_status"])
    want_galileo = bool(constellations) and "galileo" in constellations
    galileo_t0, galileo_done, n_acks = None, False, 0
    ser, pending, connected_once, down_since = None, b"", False, None
    last_bytes = last_epoch = time.monotonic()
    no_data_flagged = False
    t0 = time.monotonic()
    log.info("live: port %s @ %d baud, rate %d Hz, out %s", port, baud, rate, out_dir)

    def mono():
        return time.monotonic() * clock_scale     # the camera frames use the same clock

    def drain_events():
        while pending_events:
            kind, data = pending_events.popleft()
            writer.event(kind, **data)
            log.warning("DEMO INJECTION %s: %s", data["action"], {k: x for k, x in data.items() if k != "action"})
        if overlay is not None:
            for kind, data in overlay.events:
                writer.event(kind, **data)
            overlay.events.clear()

    def handle(verdicts):
        nonlocal last_epoch, no_data_flagged, galileo_t0, galileo_done, ser, pending
        drain_events()
        for v in verdicts:
            hub.update(v, nav.breadcrumbs() if nav else None)
            last_epoch = time.monotonic()
            if no_data_flagged:
                writer.event("data_resumed", t_gnss=v["t_gnss"])
                no_data_flagged = False
            ep_t = pipe.builder.parser.cur_time
            verdict = setup.observe(ep_t)
            if verdict:
                writer.event("rate_measured", measured_hz=setup.measured, requested_hz=setup.requested)
            if verdict == "fallback":
                writer.event("rate_fallback", measured_hz=setup.measured)
                ser = setup.fallback(ser, serial, port, lv["read_timeout_s"])
                pending = b""
            if want_galileo and not galileo_done and ep_t is not None:
                galileo_t0 = galileo_t0 or ep_t.timestamp()
                if "GA" in pipe.builder.parser.gsv_talkers:
                    writer.event("galileo_confirmed", t_gnss=v["t_gnss"])
                    log.info("Galileo confirmed: GAGSV received")
                    galileo_done = True
                elif ep_t.timestamp() - galileo_t0 > cfg["l86"]["galileo_check_s"]:
                    writer.event("galileo_missing", t_gnss=v["t_gnss"])
                    log.warning("PMTK353 sent but no GAGSV after %s s: module firmware may not support Galileo",
                                cfg["l86"]["galileo_check_s"])
                    galileo_done = True

    stop_on_sigterm()
    try:
        while duration_s is None or time.monotonic() - t0 < duration_s:
            if ser is None:
                try:
                    ser = setup.open(serial, port, lv["read_timeout_s"])
                    log.info("serial %s open%s", port, " (reconnected)" if connected_once else "")
                    writer.event("serial_open", port=port, reconnect=connected_once)
                    connected_once, down_since, pending, last_bytes = True, None, b"", time.monotonic()
                    galileo_t0 = None
                except (serial.SerialException, OSError, ValueError) as e:
                    if down_since is None:
                        log.warning("serial %s unavailable: %s; retrying every %.1f s", port, e, lv["reconnect_s"])
                        writer.event("serial_unavailable", port=port, error=str(e))
                        down_since = time.monotonic()
                    time.sleep(lv["reconnect_s"])
                    continue
            if time.monotonic() - last_bytes > lv["silence_reopen_s"]:
                log.warning("no bytes from %s for %.0f s (L86 unplugged or not powered?); reopening",
                            port, time.monotonic() - last_bytes)
                writer.event("no_serial_data", port=port, silent_s=round(time.monotonic() - last_bytes, 1))
                ser.close()
                ser, last_bytes = None, time.monotonic()
                continue
            try:
                chunk = ser.read(ser.in_waiting or 1)     # return as soon as bytes arrive: each line stamped on arrival
            except (serial.SerialException, OSError) as e:
                log.warning("serial %s lost: %s", port, e)
                writer.event("serial_lost", port=port, error=str(e))
                try:
                    ser.close()
                except Exception:
                    pass
                ser, down_since, pending = None, time.monotonic(), b""
                handle(pipe.flush())
                continue
            host = datetime.now(timezone.utc)
            if chunk:
                last_bytes = time.monotonic()
                pending += chunk
                *lines, pending = pending.split(b"\n")
                if len(pending) > 1024:                 # no newline for 1 kB: garbage
                    pending = b""
                m = mono()
                for bl in lines:
                    line = bl.decode("ascii", errors="replace").strip()
                    raw.write(line, host)
                    if injector is not None:              # demo only: the detector sees the modified line
                        line = injector.apply(line)
                        det_raw.write(line, host)
                    handle(pipe.feed(line, host, m))
                raw.flush()
                if det_raw is not None:
                    det_raw.flush()
            else:
                handle(pipe.flush())                    # the burst is over: emit the epoch now
            if len(pipe.builder.parser.acks) > n_acks:
                for cmd, flag in pipe.builder.parser.acks[n_acks:]:
                    status = {"3": "ok", "2": "failed", "1": "unsupported", "0": "invalid"}.get(flag, flag)
                    log.info("L86 ack PMTK%s: %s", cmd, status)
                    writer.event("pmtk_ack", command=cmd, status=status)
                n_acks = len(pipe.builder.parser.acks)
            if not no_data_flagged and (time.monotonic() - last_epoch) * clock_scale > lv["no_data_s"]:
                writer.event("no_data", since_s=round((time.monotonic() - last_epoch) * clock_scale, 1))
                no_data_flagged = True
    except KeyboardInterrupt:
        log.info("stopped by user")
    finally:
        if injector is not None and injector.active is not None:
            on_inject("stop", injector.stop())
        if overlay is not None:
            overlay.stop.set()
            overlay.join(timeout=2)
            writer.event("overlay_stopped", frames=overlay.frames, fps=overlay.fps, render_ms=overlay.render_ms,
                         error=overlay.error)
        if server is not None:
            server.close()
        if cam_thread is not None:
            cam_thread.stop.set()
            cam_thread.join(timeout=2)
            writer.event("camera_stopped", frames=cam_thread.frames, fps=cam_thread.fps, error=cam_thread.error)
        if ser is not None:
            ser.close()
        handle(pipe.flush())
        drain_events()
        writer.close()
        raw.close()
        if det_raw is not None:
            det_raw.close()
        log.info("live stopped: %s", pipe.summary())
    return pipe.summary()
