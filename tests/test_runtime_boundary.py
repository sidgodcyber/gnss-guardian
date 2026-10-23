"""The edge module is the Pi alone: the runtime package must not reach the network or the phone.

Checked statically over every module in gnss_guardian/: no network or phone-sensor imports, no
offline-reference code (GnssLogger parser, compare) in the runtime path.
The only socket the runtime touches is a pyserial URL the user passes (the simulator on a laptop).
"""
import ast
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "gnss_guardian"
FORBIDDEN = {"socket", "requests", "urllib", "http", "aiohttp", "websocket", "websockets", "paho",
             "gnsslogger", "compare", "phyphox"}


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
        if hits:
            bad[str(py.relative_to(PKG))] = sorted(hits)
    assert not bad, bad
