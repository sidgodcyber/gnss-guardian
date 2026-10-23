"""Session folder: everything a run produces, re-playable and readable by status / the dashboard.

    session/<name>/session.json   run info + home point
    session/<name>/epochs.jsonl   one verdict per epoch (features, scores, levels, reasons)
    session/<name>/events.jsonl   level transitions, home set, serial and receiver events
    session/<name>/latest.json    the newest verdict, replaced atomically
    session/<name>/raw/           raw NMEA with host-time prefix (live mode)
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for _ in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:          # Windows: a reader has the file open for a moment
            time.sleep(0.02)


class SessionWriter:
    def __init__(self, folder: str | Path, info: dict, append: bool = False, latest_min_s: float = 0.25):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        self.info = {"created_utc": utc_now(), **info, "home": info.get("home")}
        self._epochs = open(self.folder / "epochs.jsonl", mode, encoding="utf-8")
        self._events = open(self.folder / "events.jsonl", mode, encoding="utf-8")
        self.latest_min_s = latest_min_s
        self._latest_wall = 0.0
        self._last: dict | None = None
        self.n_epochs = self.n_events = 0
        self._write_info()

    def _write_info(self) -> None:
        write_atomic(self.folder / "session.json", json.dumps(self.info, indent=1))

    def set_home(self, home: dict) -> None:
        self.info["home"] = home
        self._write_info()

    def epoch(self, v: dict) -> None:
        line = json.dumps(v, separators=(",", ":"))
        self._epochs.write(line + "\n")
        self._epochs.flush()
        self.n_epochs += 1
        self._last = v
        now = time.monotonic()
        if now - self._latest_wall >= self.latest_min_s:
            write_atomic(self.folder / "latest.json", line)
            self._latest_wall = now

    def event(self, kind: str, **data) -> dict:
        ev = {"kind": kind, "host_utc": utc_now(), **data}
        self._events.write(json.dumps(ev, separators=(",", ":")) + "\n")
        self._events.flush()
        self.n_events += 1
        return ev

    def close(self) -> None:
        if self._last is not None:
            write_atomic(self.folder / "latest.json", json.dumps(self._last, separators=(",", ":")))
        self._epochs.close()
        self._events.close()


def read_jsonl(path: str | Path) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out
