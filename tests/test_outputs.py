"""Status LEDs and the Uno safe-action output (gnss_guardian/outputs.py, firmware/uno_safe_action,
tools/fake_uno.py).

  patterns       every state lights the right LEDs (fake backend, fake clock)
  never blocks   update() returns at once even when the LED backend is slow; a failing backend is dropped
                 with an outputs_error event, detection carries on
  import guard   no gpiozero (laptop): LEDs are a no-op, event says so
  Uno            line protocol with checksum; heartbeat carries the mode per state; the fake Uno (the
                 firmware's logic) boots SAFE, follows the heartbeat and drops to SAFE 3 s after it stops
  live           run_live against the serial simulator drives the LEDs (green after warm-up, blue blinking
                 in a jam)
"""
import copy
import importlib.util
import subprocess
import sys
import time
from collections import deque

import pytest
from conftest import ROOT, START, _load_tool

from gnss_guardian.outputs import (LEDS, FakeLeds, NullLeds, Outputs, led_pattern, level_at, parse_uno,
                                   uno_line)

fake_uno = _load_tool("fake_uno")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(cfg, leds=None, safe=None, safe_action=False):
    clk = Clock()
    out = Outputs(cfg, events=deque(), leds=leds or FakeLeds(), safe=safe, safe_action=safe_action, clock=clk,
                  start=False)
    return out, clk


def lit(out, clk, state, cause=None, at=0.0):
    out.update({"state": state, "cause": cause})
    clk.t += at
    out.step()
    return {k for k in LEDS if out.leds.level[k]}


@pytest.mark.parametrize("state,cause,on", [
    ("WARMING_UP", None, {"yellow"}), ("GNSS_OK", None, {"green"}), ("SUSPECT", None, {"yellow"}),
    ("SPOOFED", "spoofing", {"red"}), ("JAMMED", "jamming", {"blue"}),
    ("VISION_NAV", "spoofing", {"red", "green"}), ("RETURN_HOME", "jamming", {"blue", "green"}),
    ("HOME_REACHED", "spoofing", {"red", "green"}),
])
def test_state_patterns_at_start(cfg, state, cause, on):
    out, clk = make(cfg)
    assert lit(out, clk, state, cause) == on


def test_blinking(cfg):
    out, clk = make(cfg)
    assert lit(out, clk, "JAMMED", "jamming") == {"blue"}
    assert lit(out, clk, "JAMMED", "jamming", at=0.3) == set()             # 2 Hz: off after 0.25 s
    assert lit(out, clk, "JAMMED", "jamming", at=0.3) == {"blue"}
    p = led_pattern("HOME_REACHED", "spoofing")["green"]
    assert [level_at(p, t) for t in (0.0, 0.2, 0.35, 0.6, 1.2)] == [True, False, True, False, False]


def test_stale_means_yellow_fast_blink_and_safe(cfg_copy):
    cfg_copy["outputs"]["safe_action"] = False
    out, clk = make(cfg_copy)
    lit(out, clk, "GNSS_OK")
    assert out.mode == "OK"
    clk.t += cfg_copy["live"]["no_data_s"] + 0.1                 # no epoch since
    out.step()
    assert {k for k in LEDS if out.leds.level[k]} == {"yellow"} and out.mode == "SAFE"


def test_update_never_blocks_on_slow_leds(cfg):
    class SlowLeds(FakeLeds):
        def set(self, led, on):
            time.sleep(0.3)
            super().set(led, on)
    out = Outputs(cfg, events=deque(), leds=SlowLeds(), safe_action=False)     # real worker thread
    try:
        t0 = time.perf_counter()
        for i in range(200):
            out.update({"state": ("GNSS_OK", "JAMMED")[i % 2], "cause": None})
        assert time.perf_counter() - t0 < 0.05
    finally:
        out.close()


def test_failing_backend_is_dropped_with_event(cfg):
    class BadLeds(FakeLeds):
        def set(self, led, on):
            raise OSError("GPIO busy")
    out, clk = make(cfg, leds=BadLeds())
    out.update({"state": "GNSS_OK"})
    out.step()
    assert isinstance(out.leds, NullLeds)
    assert any(k == "outputs_error" and "GPIO busy" in d["error"] for k, d in out.events)
    out.update({"state": "SPOOFED", "cause": "spoofing"})      # keeps going
    out.step()


@pytest.mark.skipif(importlib.util.find_spec("gpiozero") is not None, reason="gpiozero installed (a Pi)")
def test_no_gpiozero_is_a_noop(cfg):
    out = Outputs(cfg, events=deque(), safe_action=False, start=False)
    assert isinstance(out.leds, NullLeds)
    assert any(k == "outputs_leds" and d["backend"] == "none" for k, d in out.events)
    out.update({"state": "GNSS_OK"})
    out.step()


def test_uart_pins_refused(cfg_copy):
    pytest.importorskip("gpiozero")
    from gnss_guardian.outputs import GpioLeds
    with pytest.raises(ValueError):
        GpioLeds({"green": 14, "yellow": 27, "red": 22, "blue": 23})


def test_uno_line_protocol():
    raw = uno_line(7, "HOLD", 90, True, 3000)
    assert raw.startswith(b"$GG,7,HOLD,90,1,3000*") and raw.endswith(b"\n")
    uno = fake_uno.FakeUno(0.0)
    ack = uno.feed(raw, 0.0).decode()
    assert parse_uno(ack) == {"kind": "GA", "seq": 7, "mode": "HOLD"}
    assert (uno.mode, uno.servo, uno.relay) == ("HOLD", 90, True)
    assert uno.feed(raw.replace(b"HOLD", b"HOLX"), 0.1) == b""              # bad checksum: ignored
    assert parse_uno("$GA,1,OK*00") is None


class LinkToFakeUno:
    """The Outputs' safe backend wired straight to FakeUno on the test clock."""
    def __init__(self, clk):
        self.clk, self.uno, self.acks = clk, fake_uno.FakeUno(clk()), b""

    def send(self, seq, mode, servo_deg, relay, watchdog_ms):
        self.acks += self.uno.feed(uno_line(seq, mode, servo_deg, relay, watchdog_ms), self.clk())

    def poll(self):
        lines, self.acks = self.acks.decode().splitlines(), b""
        return [m for m in map(parse_uno, lines) if m]

    def close(self):
        pass


def test_uno_follows_state_and_watchdog(cfg_copy):
    cfg_copy["outputs"]["safe_action"] = True
    clk = Clock()
    link = LinkToFakeUno(clk)
    out = Outputs(cfg_copy, events=deque(), leds=FakeLeds(), safe=link, clock=clk, start=False)
    assert link.uno.mode == "SAFE"                                     # power-up state
    seen = []
    for state, cause in (("WARMING_UP", None), ("GNSS_OK", None), ("SPOOFED", "spoofing"), ("VISION_NAV", "spoofing"),
                         ("GNSS_OK", None)):
        for _ in range(3):                                             # 3 heartbeats per state
            out.update({"state": state, "cause": cause})
            out.step()
            clk.t += cfg_copy["outputs"]["uno"]["heartbeat_s"]
            link.uno.tick(clk.t)
        seen.append((link.uno.mode, link.uno.relay, link.uno.servo))
    assert seen == [("HOLD", True, 90), ("OK", True, 0), ("SAFE", False, 180), ("HOLD", True, 90), ("OK", True, 0)]
    assert any(k == "uno_link" and d["ok"] for k, d in out.events)
    # the Pi hangs: no more heartbeats -> the Uno goes SAFE on its own after watchdog_s (the loop has
    # already advanced 1 s past the last heartbeat)
    clk.t += 1.5
    link.uno.tick(clk.t)
    assert link.uno.mode == "OK"                                       # 2.5 s: still within the watchdog
    clk.t += 1.0
    link.uno.tick(clk.t)
    assert (link.uno.mode, link.uno.relay) == ("SAFE", False)


def test_mode_change_sent_without_waiting_for_heartbeat(cfg_copy):
    clk = Clock()
    link = LinkToFakeUno(clk)
    out = Outputs(cfg_copy, events=deque(), leds=FakeLeds(), safe=link, safe_action=True, clock=clk, start=False)
    out.update({"state": "GNSS_OK"})
    out.step()
    clk.t += 0.05
    out.update({"state": "SPOOFED", "cause": "spoofing"})
    out.step()                                                 # 0.05 s later, heartbeat not due for 0.95 s
    assert (link.uno.mode, link.uno.relay) == ("SAFE", False)


def test_safe_action_off_by_default(cfg):
    assert cfg["outputs"]["safe_action"] is False
    out = Outputs(cfg, events=deque(), leds=FakeLeds(), start=False)
    assert out.safe is None


def test_uno_over_serial_url_and_watchdog(cfg_copy):
    """Real UnoSafe (pyserial socket://) against tools/fake_uno.py: heartbeat acknowledged, and the fake Uno
    drops to SAFE by itself once the heartbeat stops without a goodbye (a hung Pi)."""
    p = subprocess.Popen([sys.executable, str(ROOT / "tools" / "fake_uno.py")], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True)
    try:
        port = p.stdout.readline().split()[1]
        cfg_copy["outputs"]["uno"]["port"] = port
        out = Outputs(cfg_copy, events=deque(), leds=FakeLeds(), safe_action=True)
        t_end = time.monotonic() + 2.5
        while time.monotonic() < t_end:
            out.update({"state": "GNSS_OK", "cause": None})
            time.sleep(0.1)
        assert any(k == "uno_link" and d["ok"] for k, d in out.events), list(out.events)
        out._stop.set()                                    # the worker dies; nothing tells the Uno
        out.thread.join(timeout=1)
        t_stop = time.monotonic()
        lines = []
        while time.monotonic() - t_stop < 5:
            line = p.stdout.readline()
            lines.append(line)
            if "mode SAFE" in line and len(lines) > 1:
                break
        safe_at = time.monotonic() - t_stop
        assert "mode SAFE" in lines[-1] and 2.0 <= safe_at <= 4.5, (lines, safe_at)
    finally:
        p.kill()
        p.wait(timeout=5)


def test_live_drives_leds(tmp_path, cfg):
    sim = _load_tool("fake_l86")
    c = copy.deepcopy(cfg)
    c["live"]["reconnect_s"] = 0.1
    p = subprocess.Popen([sys.executable, str(ROOT / "tools" / "fake_l86.py"), "--transport", "tcp", "--speed", "20",
                          "--start", START, "--duration-s", "110", "--jam", "sudden", "--jam-at", "90"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    leds = FakeLeds()
    out = Outputs(c, leds=leds, safe_action=False)
    try:
        port = p.stdout.readline().split()[1]
        from gnss_guardian.io.serial_live import run_live
        run_live(port, 9600, tmp_path, c, clock_scale=20, duration_s=110 / 20 + 1.5, outputs=out)
    finally:
        p.kill()
        p.wait(timeout=10)
    assert sim is not None
    on = [led for led, v in leds.changes if v]
    assert on[0] == "yellow" and "green" in on                   # warming up, then GNSS_OK
    assert on.count("blue") >= 2                                 # JAMMED: blinking
    assert not any(leds.level.values())                          # close(): all off
