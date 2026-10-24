"""Display server inside live mode (the camera can be opened only once, so the stream comes from the
live process itself). Display only: it serves what LiveHub holds and never feeds the pipeline,
except the demo-injection controls when live runs with --demo-inject.

    http://<pi>:PORT/               camera overlay, MJPEG (multipart/x-mixed-replace), ~5 fps, 320x240
    http://<pi>:PORT/overlay.jpg    the newest overlay frame
    http://<pi>:PORT/guide          guidance page for the person walking the rig (phone or laptop)
    http://<pi>:PORT/live.json      the live view (also written to session/<name>/live.json)
    POST /inject/jump?m=30&bearing=90     demo injection (only with --demo-inject)
    POST /inject/drift?mps=0.5&bearing=90
    POST /inject/stop

It listens on the local network only for viewers; nothing is sent anywhere. If the port cannot be
opened, start() returns False and live mode carries on without it.
"""
from __future__ import annotations

import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from gnss_guardian.web.pages import GUIDE_HTML

log = logging.getLogger("gnss_guardian.web")
BOUNDARY = "gnssguardianframe"


def local_addresses() -> list[str]:
    """This machine's LAN IPv4 addresses (for the URLs printed at start-up); no packet leaves the host."""
    out = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))          # TEST-NET address: picks the outgoing interface, sends nothing
        out.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127.") and ip not in out:
                out.append(ip)
    except OSError:
        pass
    return out or ["127.0.0.1"]


class LiveServer:
    def __init__(self, hub, port: int, host: str = "0.0.0.0", on_inject=None):
        self.hub, self.port, self.host = hub, port, host
        self.on_inject = on_inject              # callback(kind, result) -> session event (live mode)
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.error: str | None = None

    def start(self) -> bool:
        server = self

        class Handler(_Handler):
            live = server
        try:
            self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        except OSError as e:
            self.error = str(e)
            log.warning("display server: cannot listen on port %d (%s); live mode continues without it", self.port, e)
            return False
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.25},
                                       daemon=True, name="display-server")
        self.thread.start()
        return True

    def urls(self) -> dict:
        ip = local_addresses()[0]
        base = f"http://{ip}:{self.port}"
        return {"overlay": f"{base}/", "guide": f"{base}/guide", "live_json": f"{base}/live.json"}

    def close(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()


class _Handler(BaseHTTPRequestHandler):
    live: LiveServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):          # keep the live console readable
        log.debug("display %s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")     # the dashboard on the laptop reads live.json
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data) -> None:
        self._send(code, json.dumps(data).encode(), "application/json")

    def do_GET(self):
        path = urlsplit(self.path).path
        hub = self.live.hub
        if path in ("/", "/stream.mjpg"):
            return self._stream()
        if path == "/overlay.jpg":
            _, jpg = hub.wait_jpeg(-1, timeout=0)
            return self._send(200, jpg, "image/jpeg") if jpg else self._send(503, b"no frame yet", "text/plain")
        if path in ("/guide", "/guide/"):
            return self._send(200, GUIDE_HTML.encode(), "text/html; charset=utf-8")
        if path == "/live.json":
            v = hub.view()
            return self._json(200, v) if v else self._json(503, {"error": "no epoch yet"})
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        u = urlsplit(self.path)
        if not u.path.startswith("/inject/"):
            return self._send(404, b"not found", "text/plain")
        inj = self.live.hub.injector
        if inj is None:
            return self._json(403, {"error": "demo injection is off: start live with --demo-inject"})
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        kind = u.path[len("/inject/"):]
        try:
            if kind == "stop":
                result = inj.stop()
            elif kind == "jump":
                result = inj.start("jump", float(q.get("bearing", 0)), m=float(q.get("m", 0)))
            elif kind == "drift":
                result = inj.start("drift", float(q.get("bearing", 0)), mps=float(q.get("mps", 0)))
            else:
                return self._json(404, {"error": f"unknown injection {kind!r}"})
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        if self.live.on_inject is not None:
            self.live.on_inject(kind, result)
        self._json(200, {"ok": True, "kind": kind, "injection": inj.status()})

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.end_headers()
        seq = -1
        try:
            while True:
                seq, jpg = self.live.hub.wait_jpeg(seq, timeout=2.0)
                if jpg is None:
                    continue
                self.wfile.write(f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpg)}\r\n\r\n"
                                 .encode() + jpg + b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass                                  # the viewer went away
        self.close_connection = True
