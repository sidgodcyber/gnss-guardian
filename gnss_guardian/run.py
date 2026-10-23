"""The detection pipeline shared by live mode and replay: NMEA lines -> epochs -> verdicts ->
session folder (epochs, events, latest, home).

    gnss-guardian replay FILE.nmea --out session/NAME [--speed 0|1|N] [--home lat,lon]

Replay reads recorded NMEA (host-prefixed from tools/record_nmea.py, IonWatch RawNmeaLog or
fake_l86 --write; plain NMEA works too but then has no host clock for the time check) and
prints the event timeline.
"""
from __future__ import annotations

import time
from pathlib import Path

from gnss_guardian.detect.engine import DetectionEngine
from gnss_guardian.detect.features import epoch_time
from gnss_guardian.home import HomeSetter
from gnss_guardian.io.epochs import EpochBuilder
from gnss_guardian.io.nmea import split_prefix
from gnss_guardian.session import SessionWriter

DETECTORS = ("jamming", "spoofing")


class Pipeline:
    def __init__(self, cfg: dict, writer: SessionWriter, home: tuple[float, float] | None = None, echo=None):
        self.cfg = cfg
        self.builder = EpochBuilder(cfg)
        self.engine = DetectionEngine(cfg)
        self.home = HomeSetter(cfg, home)
        self.writer = writer
        self.echo = echo or (lambda s: None)
        self.transitions: list[dict] = []
        self.counts = {"epochs": 0, **{f"{d}_alarm_epochs": 0 for d in DETECTORS},
                       **{f"{d}_suspect_epochs": 0 for d in DETECTORS}}
        self._warm = True
        self._antenna = None
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
        v = self.engine.update(ep)
        t = epoch_time(ep)
        new_home = self.home.update(v, t)
        if new_home:
            self.writer.set_home(new_home)
            self.writer.event("home_set", t_gnss=v["t_gnss"], **new_home)
            self.echo(f"{v['t_gnss']}  home set: {new_home['lat']:.6f}, {new_home['lon']:.6f} ({new_home['source']})")
        en = self.home.enu(v["lat"], v["lon"])
        v["enu_m"] = [round(en[0], 2), round(en[1], 2)] if en else None
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
        self.writer.epoch(v)
        return v

    def summary(self) -> dict:
        first = {}
        for tr in self.transitions:
            if tr["level"] == "ALARM" and tr["detector"] not in first:
                first[tr["detector"]] = tr["t_gnss"]
        return {**self.counts, "first_alarm": first, "transitions": len(self.transitions),
                "home": self.home.home}


def replay(path: str | Path, cfg: dict, out: str | Path, speed: float = 0.0,
           home: tuple[float, float] | None = None, echo=None) -> dict:
    """Push a recorded NMEA file through the pipeline. speed 0 = as fast as possible, else
    N x real time (paced by the host prefix, or by GNSS time when there is none)."""
    writer = SessionWriter(out, {"mode": "replay", "source": str(path), "node_id": cfg["node_id"],
                                 "speed": speed})
    pipe = Pipeline(cfg, writer, home, echo)
    t0 = wall0 = None
    with open(path, encoding="ascii", errors="replace") as f:
        for line in f:
            if speed > 0:
                _, host, _ = split_prefix(line)
                t = host.timestamp() if host else (pipe.builder.parser.cur_time.timestamp()
                                                   if pipe.builder.parser.cur_time else None)
                if t is not None:
                    if t0 is None:
                        t0, wall0 = t, time.monotonic()
                    while (rem := wall0 + (t - t0) / speed - time.monotonic()) > 0:
                        time.sleep(min(rem, 0.25))
            pipe.feed(line)
    pipe.finish()
    return pipe.summary()
