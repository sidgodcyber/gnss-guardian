"""LiveHub: what live mode shows while it runs, shared by the serial loop (one update per epoch), the
overlay thread (camera view) and the display server (overlay stream, guidance page, live.json).

    hub = LiveHub(cfg, out_dir, injector=None, vision_fps=lambda: cam_thread.fps)
    hub.update(verdict)        # every epoch: builds the live view, writes session/<name>/live.json
    hub.view()                 # the newest live view (dict), as served at /live.json
    hub.publish_jpeg(jpg)      # the overlay thread's newest frame; hub.wait_jpeg(seq) for the stream

live.json (gnss-guardian-live/1, replaced atomically once per epoch): state, detector levels, scores
and reasons, fix, satellites, HDOP, mean C/N0, guidance, vision rate and mode, home and home keyframe
status, demo injection status, the dashboard row of this epoch, and the last 60 s of the fused track
in metres from home. No latitude or longitude.
"""
from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from pathlib import Path

from gnss_guardian.dashboard_export import _row, indicator_config
from gnss_guardian.session import utc_now, write_atomic

FORMAT = "gnss-guardian-live/1"
TRACK_S = 60.0                 # fused track in live.json
MAP_S = 600.0                  # GNSS / vision tracks kept for the overlay's inset map


def _r(x, nd=2):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(x, nd)


class LiveHub:
    def __init__(self, cfg: dict, out_dir: str | Path | None = None, injector=None, vision_fps=None,
                 write_file: bool = True):
        self.cfg = cfg
        self.out_dir = Path(out_dir) if out_dir else None
        self.injector = injector
        self.vision_fps = vision_fps or (lambda: None)
        self.write_file = write_file and self.out_dir is not None
        self.config = indicator_config(cfg)
        self._lock = threading.Lock()
        self._view: dict | None = None
        self._t0: float | None = None
        self._last_update = None
        self.track: deque = deque()            # (t, e, n, source) fused
        self.gnss_track: deque = deque()       # (t, e, n) GNSS fixes (trusted or not)
        self.vision_track: deque = deque()     # (t, e, n) camera position
        self.keyframes: list = []              # (e, n, home)
        self._jpeg: bytes | None = None
        self._jpeg_seq = 0
        self._jpeg_cond = threading.Condition()

    # ---------------------------------------------------------------- epochs (serial thread)
    def update(self, v: dict, keyframes: list | None = None) -> dict:
        t = v.get("mono")
        t = float(t) if t is not None else time.monotonic()
        if self._t0 is None:
            self._t0 = t
        rel = t - self._t0
        fu, enu, vis = v.get("fused"), v.get("enu_m"), v.get("vision") or {}
        with self._lock:
            if fu:
                self.track.append((rel, fu["e"], fu["n"], fu["source"]))
            if enu and v.get("fix_valid"):
                self.gnss_track.append((rel, enu[0], enu[1]))
            if vis.get("e") is not None:
                self.vision_track.append((rel, vis["e"], vis["n"]))
            for d, keep in ((self.track, TRACK_S), (self.gnss_track, MAP_S), (self.vision_track, MAP_S)):
                while d and d[0][0] < rel - keep:
                    d.popleft()
            if keyframes is not None:
                self.keyframes = [(e, n, home) for _, e, n, home in keyframes]
            view = self._build(v, rel, vis)
            self._view = view
            self._last_update = time.monotonic()
        if self.write_file:
            write_atomic(self.out_dir / "live.json", json.dumps(view, separators=(",", ":")))
        return view

    def _build(self, v: dict, rel: float, vis: dict) -> dict:
        f = v.get("features") or {}
        inj = self.injector.status() if self.injector is not None else {"enabled": False, "active": None}
        row = _row(v, rel)
        row["injection"] = inj["active"]
        return {
            "format": FORMAT, "written_utc": utc_now(), "node_id": self.cfg["node_id"],
            "session": self.out_dir.name if self.out_dir else None,
            "seq": v.get("seq"), "t": _r(rel, 3), "t_gnss": v.get("t_gnss"),
            "state": v.get("state"), "cause": v.get("cause"), "labels": v.get("labels"),
            "warming_up": v.get("warming_up"), "gnss_trusted": v.get("gnss_trusted"),
            "jamming": {k: v["jamming"][k] for k in ("level", "score", "reasons")},
            "spoofing": {k: v["spoofing"][k] for k in ("level", "score", "reasons")},
            "fix": {"valid": v.get("fix_valid"), "sats_used": f.get("n_used"), "sats_tracked": f.get("n_tracked"),
                    "hdop": v.get("hdop"), "cn0_mean": _r(f.get("cn0_mean"), 1)},
            "fused": v.get("fused"), "guidance": v.get("guidance"), "latch": v.get("latch"),
            "jump_check": v.get("jump_check"),
            "home": v.get("home_status"), "home_keyframe": vis.get("home_kf"),
            "vision": {"fps": self.vision_fps(), "mode": vis.get("mode"), "aligned": vis.get("aligned"),
                       "heading": vis.get("heading"), "quality": vis.get("quality"), "flag": vis.get("flag"),
                       "keyframes": vis.get("keyframes"), "home_confirmed": vis.get("home_confirmed"),
                       "e": vis.get("e"), "n": vis.get("n")} if vis else None,
            "injection": inj,
            "track": [[_r(t, 1), e, n, s] for t, e, n, s in self.track],
            "keyframes": [[_r(e), _r(n), home] for e, n, home in self.keyframes],
            "row": row, "config": self.config,
        }

    # ---------------------------------------------------------------- readers (other threads)
    def view(self) -> dict | None:
        with self._lock:
            if self._view is None:
                return None
            return {**self._view, "age_s": _r(time.monotonic() - self._last_update, 1)}

    def map_snapshot(self) -> dict:
        with self._lock:
            return {"gnss": [(e, n) for _, e, n in self.gnss_track], "vision": [(e, n) for _, e, n in self.vision_track],
                    "keyframes": list(self.keyframes)}

    def publish_jpeg(self, jpg: bytes) -> None:
        with self._jpeg_cond:
            self._jpeg, self._jpeg_seq = jpg, self._jpeg_seq + 1
            self._jpeg_cond.notify_all()

    def wait_jpeg(self, after: int, timeout: float = 2.0) -> tuple[int, bytes | None]:
        """(seq, jpeg) of the first frame newer than ``after`` (or the current one on timeout)."""
        with self._jpeg_cond:
            self._jpeg_cond.wait_for(lambda: self._jpeg_seq > after, timeout=timeout)
            return self._jpeg_seq, self._jpeg
