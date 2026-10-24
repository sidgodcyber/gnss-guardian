"""The pipeline shared by live mode and replay: NMEA lines -> epochs -> verdicts -> vision ->
navigation state and guidance (Phase 3) -> session folder (epochs, events, latest, home).

    gnss-guardian replay FILE.nmea --out session/NAME [--speed 0|1|N] [--home lat,lon]
                                   [--video video.avi [--frames frames.csv]]

Replay reads recorded NMEA (host-prefixed from tools/record_nmea.py / record_session.py, IonWatch
RawNmeaLog or fake_l86 --write; plain NMEA works too but then has no host clock for the time check)
and prints the event timeline. With --video the camera frames are merged with the NMEA lines in
host-time order, so vision runs exactly as it did live.
"""
from __future__ import annotations

import math
import time
from pathlib import Path

from gnss_guardian.detect.engine import DetectionEngine
from gnss_guardian.detect.features import epoch_time
from gnss_guardian.detect.spoofing import JUMP_AFTER_DEGRADED
from gnss_guardian.home import HomeSetter
from gnss_guardian.io.epochs import EpochBuilder
from gnss_guardian.io.nmea import split_prefix
from gnss_guardian.session import SessionWriter
from gnss_guardian.state import StateMachine
from gnss_guardian.vision.nav import VisionNav

DETECTORS = ("jamming", "spoofing")


class Pipeline:
    def __init__(self, cfg: dict, writer: SessionWriter, home: tuple[float, float] | None = None, echo=None,
                 vision: VisionNav | None = None):
        self.cfg = cfg
        self.vision = vision
        self._home_vision = None
        self.builder = EpochBuilder(cfg)
        self.engine = DetectionEngine(cfg)
        if vision is not None:
            self.engine.vision_track = vision.displacement     # 30 s GNSS-vs-camera displacement check
        self.state = StateMachine(cfg, camera=vision is not None)
        self.home = HomeSetter(cfg, home)
        self.writer = writer
        self.echo = echo or (lambda s: None)
        self.transitions: list[dict] = []
        self.states: list[dict] = []
        self.counts = {"epochs": 0, **{f"{d}_alarm_epochs": 0 for d in DETECTORS},
                       **{f"{d}_suspect_epochs": 0 for d in DETECTORS}}
        self._warm = True
        self._antenna = None
        self._last_enu: list | None = None          # newest valid fix, metres from home
        self._home_wait: str | None = None
        if self.home.home is not None:
            writer.set_home(self.home.home)

    def feed(self, line: str, host=None, mono=None) -> list[dict]:
        return [self._handle(ep) for ep in self.builder.feed(line, host, mono)]

    def flush(self) -> list[dict]:
        return [self._handle(ep) for ep in self.builder.flush()]

    def finish(self) -> list[dict]:
        out = self.flush()
        self.writer.close()
        return out

    def _handle(self, ep) -> dict:
        if self.vision is not None:
            vel = self.vision.velocity()
            if vel is not None:
                self.engine.observe_vision(*vel)
        v = self.engine.update(ep)
        if self.state.gnss_blocked and v["gnss_trusted"]:
            v["gnss_trusted"] = False                # spoof latch held until GNSS and the camera agree
            v["trust_blocked"] = "spoof latch: waiting for GNSS and camera to agree"
        t = epoch_time(ep)
        new_home = self.home.update(v, t)
        if new_home:
            self.writer.set_home(new_home)
            self.writer.event("home_set", t_gnss=v["t_gnss"], **new_home)
            self.echo(f"{v['t_gnss']}  home set: {new_home['lat']:.6f}, {new_home['lon']:.6f} ({new_home['source']}, "
                      f"spread {new_home['spread_m']} m, HDOP {new_home['hdop']}, {new_home['sats_used']:.0f} sats)")
            if self.vision is not None:
                self.vision.set_home(t)              # the rig stands on home: vision starts there
        elif self.home.waiting and self.home.waiting.startswith("home not set") and self.home.waiting != self._home_wait:
            self.writer.event("home_waiting", t_gnss=v["t_gnss"], detail=self.home.waiting)
            self.echo(f"{v['t_gnss']}  {self.home.waiting}")
        self._home_wait = self.home.waiting
        v["home_status"] = {"set": self.home.home is not None, "detail": self.home.waiting or self.home.home["source"]}
        en = self.home.enu(v["lat"], v["lon"])
        v["enu_m"] = [round(en[0], 2), round(en[1], 2)] if en else None
        if JUMP_AFTER_DEGRADED in v["spoofing"]["reasons"]:
            self._annotate_jump(t, v)
        if v["fix_valid"] and v["enu_m"] is not None:
            self._last_enu = v["enu_m"]
        self.counts["epochs"] += 1
        if self._warm and not v["warming_up"]:
            self._warm = False
            self.writer.event("warmup_done", t_gnss=v["t_gnss"], mono=v["mono"])
            self.echo(f"{v['t_gnss']}  baseline ready, detection active")
        if ep.antenna != self._antenna:
            self.writer.event("antenna", t_gnss=v["t_gnss"], status=ep.antenna, previous=self._antenna)
            if ep.antenna not in (None, "OK"):
                self.echo(f"{v['t_gnss']}  ANTENNA {ep.antenna}: hardware fault, not jamming")
            self._antenna = ep.antenna
        for d in DETECTORS:
            lvl = v[d]["level"]
            if lvl == "ALARM":
                self.counts[f"{d}_alarm_epochs"] += 1
            elif lvl == "SUSPECT":
                self.counts[f"{d}_suspect_epochs"] += 1
            if v[d]["changed"]:
                tr = {"t_gnss": v["t_gnss"], "mono": v["mono"], "seq": v["seq"], "detector": d, "level": lvl,
                      "score": v[d]["score"], "reasons": v[d]["reasons"], "labels": v["labels"]}
                self.transitions.append(tr)
                self.writer.event("level", **tr)
                self.echo(f"{v['t_gnss']}  {d:<8} -> {lvl:<7} {v[d]['score']:.2f}  {'; '.join(v[d]['reasons'])}")
        if self.vision is not None:
            self._vision_step(t, v)
        self._state_step(t, v)
        self.writer.epoch(v)
        return v

    def _annotate_jump(self, t: float, v: dict) -> None:
        """A jump right after jamming or a fix gap keeps its alarm; say whether it moved GNSS toward
        the camera position (a receiver catching up after a degraded fix) or away from it."""
        cam = self.vision.position_at(t) if self.vision is not None else None
        prev, cur = self._last_enu, v["enu_m"]
        if cam is None or prev is None or cur is None:
            v["jump_check"] = {"after_degraded": True, "toward_camera": None}
            text = f"{JUMP_AFTER_DEGRADED} (no camera position to compare with)"
        else:
            before = math.hypot(prev[0] - cam[0], prev[1] - cam[1])
            after = math.hypot(cur[0] - cam[0], cur[1] - cam[1])
            v["jump_check"] = {"after_degraded": True, "toward_camera": after < before,
                               "gnss_to_camera_before_m": round(before, 1), "gnss_to_camera_after_m": round(after, 1)}
            text = (f"{JUMP_AFTER_DEGRADED}: {'toward' if after < before else 'away from'} the camera position "
                    f"(GNSS {before:.1f} m -> {after:.1f} m from it)")
        r = v["spoofing"]["reasons"]
        r[r.index(JUMP_AFTER_DEGRADED)] = text

    def _state_step(self, t: float, v: dict) -> None:
        out = self.state.update(t, v, v.get("vision"), self.vision.breadcrumbs() if self.vision else None)
        v.update(state=out["state"], cause=out["cause"], fused=out["fused"], guidance=out["guidance"],
                 latch=out["latch"])
        if out["release_spoofing"]:
            self.engine.release_spoofing()
        if out["escalated"]:
            self.writer.event("alarm_escalated", t_gnss=v["t_gnss"], seq=v["seq"], cause=out["cause"],
                              reasons=v["spoofing"]["reasons"])
        if out["changed"]:
            tr = {"t_gnss": v["t_gnss"], "mono": v["mono"], "seq": v["seq"], "state": out["state"],
                  "from": out["previous"], "cause": out["cause"], "reasons": out["reasons"], "fused": out["fused"],
                  "guidance": out["guidance"]}
            self.states.append(tr)
            self.writer.event("state", **tr)
            self.echo(f"{v['t_gnss']}  state    -> {out['state']:<12} {'; '.join(out['reasons'])}")
        if out["route"] is not None:
            self.writer.event("guidance", t_gnss=v["t_gnss"], seq=v["seq"], route=out["route"],
                              note="advisory: reverse breadcrumb path to home, newest keyframe first")
        for w in out["passed"]:
            self.writer.event("waypoint", t_gnss=v["t_gnss"], seq=v["seq"], id=w["id"], e=w["e"], n=w["n"])

    def _vision_step(self, t: float, v: dict) -> None:
        nav = self.vision
        nav.on_gnss(t, v)
        st = nav.state(at=t)                     # vision position at this epoch's time, comparable with GNSS
        v["vision"] = st
        for kf in nav.new_keyframes:
            self.writer.event("keyframe", t_gnss=v["t_gnss"], id=kf.id, e=round(kf.e, 2), n=round(kf.n, 2),
                              heading=round(kf.heading, 1), home=kf.home, features=len(kf.pts))
            if kf.home:
                self.echo(f"{v['t_gnss']}  home keyframe stored ({len(kf.pts)} features)")
        nav.new_keyframes.clear()
        for kind, data in nav.events:
            self.writer.event(kind, t_gnss=v["t_gnss"], **data)
            self.echo(f"{v['t_gnss']}  {data.get('detail', kind)}")
        nav.events.clear()
        if v["gnss_trusted"]:
            self._home_vision = None
        elif st["home_confirmed"] and self._home_vision is None and \
                t - st["home_confirmed"]["t"] <= self.cfg["state"]["home_match_max_age_s"]:  # first fresh match per denial
            self._home_vision = st["home_confirmed"]
            self.writer.event("home_confirmed", t_gnss=v["t_gnss"], **st["home_confirmed"])
            self.echo(f"{v['t_gnss']}  home keyframe matched: {st['home_confirmed']['offset_m']} m from home")

    def summary(self) -> dict:
        first = {}
        for tr in self.transitions:
            if tr["level"] == "ALARM" and tr["detector"] not in first:
                first[tr["detector"]] = tr["t_gnss"]
        return {**self.counts, "first_alarm": first, "transitions": len(self.transitions),
                "states": [s["state"] for s in self.states], "home": self.home.home}


def replay(path: str | Path, cfg: dict, out: str | Path, speed: float = 0.0,
           home: tuple[float, float] | None = None, echo=None, video: str | Path | None = None,
           frames_csv: str | Path | None = None, video_start=None) -> dict:
    """Push a recorded NMEA file (and optionally its camera video) through the pipeline. speed 0 =
    as fast as possible, else N x real time (paced by the host prefix, or by GNSS time when there
    is none). Video frames are fed before every NMEA line that is later than them."""
    out = Path(out)
    writer = SessionWriter(out, {"mode": "replay", "source": str(path), "node_id": cfg["node_id"],
                                 "speed": speed, "video": str(video) if video else None})
    nav = cam = frame = None
    if video:
        from gnss_guardian.vision.camera import open_camera
        cam = open_camera(f"video:{video}", cfg, frames_csv=frames_csv, start=video_start)
        nav = VisionNav(cfg, keyframe_dir=out / "keyframes")
        frame = cam.read()
    pipe = Pipeline(cfg, writer, home, echo, vision=nav)
    t0 = wall0 = None
    n_frames = 0
    prev_host = None
    gap = cfg["live"]["read_timeout_s"]
    with open(path, encoding="ascii", errors="replace") as f:
        for line in f:
            _, host, _ = split_prefix(line)
            if host is not None and prev_host is not None and (host - prev_host).total_seconds() > gap:
                pipe.flush()                     # the burst ended here: live mode emits the epoch now
            prev_host = host or prev_host
            if cam is not None:
                if host is None:
                    raise ValueError(f"{path}: replay with --video needs host-prefixed NMEA to line up the frames")
                while frame is not None and frame.t <= host.timestamp():
                    nav.on_frame(frame.t, frame.gray)
                    n_frames += 1
                    frame = cam.read()
            if speed > 0:
                t = host.timestamp() if host else (pipe.builder.parser.cur_time.timestamp()
                                                   if pipe.builder.parser.cur_time else None)
                if t is not None:
                    if t0 is None:
                        t0, wall0 = t, time.monotonic()
                    while (rem := wall0 + (t - t0) / speed - time.monotonic()) > 0:
                        time.sleep(min(rem, 0.25))
            pipe.feed(line)
    pipe.finish()
    if cam is not None:
        cam.close()
    s = pipe.summary()
    if nav is not None:
        s["frames"] = n_frames
        s["keyframes"] = len(nav.map.keyframes)
    from gnss_guardian.dashboard_export import write_dashboard_json
    s["dashboard"] = str(write_dashboard_json(out, cfg, video=video, frames_csv=frames_csv, video_start=video_start))
    return s


def is_live_session(folder: str | Path) -> bool:
    """A session folder written by live mode (gnss-guardian live / field-demo)."""
    import json
    folder = Path(folder)
    try:
        info = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return info.get("mode") == "live" and (folder / "epochs.jsonl").is_file()


def export_live_session(folder: str | Path, cfg: dict) -> dict:
    """dashboard.json for a live session from what live mode recorded (its state machine, vision and
    demo injections), with the recorded overlay as the video when there is one."""
    from gnss_guardian.dashboard_export import write_dashboard_json
    from gnss_guardian.session import read_jsonl
    folder = Path(folder)
    video = next((folder / n for n in ("overlay.mp4", "overlay.avi") if (folder / n).is_file()), None)
    frames = folder / "overlay_frames.csv"
    if video is not None and not frames.is_file():
        video = None
    path = write_dashboard_json(folder, cfg, video=video, frames_csv=frames if video else None)
    eps = read_jsonl(folder / "epochs.jsonl")
    states, prev = [], None
    for v in eps:
        if v.get("state") != prev:
            states.append(v.get("state"))
            prev = v.get("state")
    return {"epochs": len(eps), "states": states, "dashboard": str(path), "video": str(video) if video else None}


def live_session_nmea(folder: str | Path, out: str | Path) -> Path:
    """One NMEA file from a live session for re-running detection: detector_input/ (what the detector saw,
    demo injections included) when present, else raw/."""
    folder = Path(folder)
    src = folder / "detector_input" if (folder / "detector_input").is_dir() else folder / "raw"
    files = sorted(src.glob("nmea_*.nmea"))
    if not files:
        raise FileNotFoundError(f"{folder}: no NMEA in raw/ or detector_input/")
    Path(out).mkdir(parents=True, exist_ok=True)
    dst = Path(out) / f"{src.name}.nmea"
    dst.write_text("".join(f.read_text(encoding="ascii", errors="replace") for f in files), encoding="ascii")
    return dst


RECORDING_NMEA = ("nmea.nmea", "gnss.nmea")      # tools/record_session.py, standalone Pi recorders
RECORDING_VIDEO = ("video.avi", "camera.avi")


def resolve_recording(folder: str | Path) -> tuple[Path, Path | None, Path | None]:
    """(nmea, video, frames.csv) inside a recording folder; video and frames are None when absent."""
    folder = Path(folder)
    nmea = next((folder / n for n in RECORDING_NMEA if (folder / n).is_file()), None)
    if nmea is None:
        raise FileNotFoundError(f"{folder}: no {' or '.join(RECORDING_NMEA)}")
    video = next((folder / n for n in RECORDING_VIDEO if (folder / n).is_file()), None)
    frames = folder / "frames.csv"
    return nmea, video, frames if frames.is_file() else None
