"""The edge module is the Pi alone: the runtime package must not reach out to the network or the phone.

Checked statically over every module in gnss_guardian/: no network-client or phone-sensor imports, no
offline-reference code (GnssLogger parser, compare) in the runtime path. The one exception is the
display server in gnss_guardian/web/ (live --overlay): it may listen (http.server, socket, URL parsing)
so a laptop or phone on the LAN can watch, but it opens no outgoing connection.
"""
import ast
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "gnss_guardian"
FORBIDDEN = {"socket", "requests", "urllib", "http", "aiohttp", "websocket", "websockets", "paho",
             "gnsslogger", "compare", "phyphox"}
SERVER_ONLY = ("socket", "http.server", "urllib.parse")          # allowed in gnss_guardian/web/ only


def allowed_in_web(name: str) -> bool:
    return any(name == a or name.startswith(a + ".") for a in SERVER_ONLY)


def imported_names(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names |= {f"{node.module}.{a.name}" for a in node.names}
    return names


def test_runtime_imports_no_network_no_phone():
    bad = {}
    for py in PKG.rglob("*.py"):
        hits = {n for n in imported_names(py) if FORBIDDEN & set(n.split("."))}
        if py.parent.name == "web":
            hits = {n for n in hits if not allowed_in_web(n)}
        if hits:
            bad[str(py.relative_to(PKG))] = sorted(hits)
    assert not bad, bad


def test_web_server_only_listens():
    for py in (PKG / "web").glob("*.py"):
        names = imported_names(py)
        assert not {n for n in names if n.startswith(("http.client", "urllib.request", "requests"))}, py
