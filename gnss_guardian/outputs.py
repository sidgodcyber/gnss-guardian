"""Hardware outputs in live mode: four status LEDs on the Pi's GPIO and, for the demo only, a safe-action
output for a ground stand-in (relay + servo flag) on an Arduino Uno over USB serial with a heartbeat
watchdog. Guidance stays advisory: the safe action is off unless outputs.safe_action (or
--safe-action) turns it on, and it only follows the navigation state.

    out = Outputs(cfg, events=deque())    # starts its own thread; never raises into the caller
    out.update(v)                         # every epoch, from the serial loop: stores the newest state only
    out.close()                           # LEDs off, Uno told SAFE

The serial loop never waits on the hardware: update() stores the state under a lock and returns. One
worker thread (outputs.tick_s) drives the LED patterns and sends the Uno heartbeat; a backend that is
slow or fails only delays or stops its own outputs (event outputs_error), never detection.

LEDs (BCM pins in outputs.pins; not GPIO14/15, the L86 UART):
  GNSS_OK                     green steady
  WARMING_UP, SUSPECT         yellow steady
  SPOOFED                     red steady
  JAMMED                      blue blinking 2 Hz (outputs.jam_led: red on a 3-LED build, blue pin null)
  VISION_NAV, RETURN_HOME     the alarm's LED (red steady / jam LED blinking) + green slow blink (camera navigating)
  HOME_REACHED                the alarm's LED + green double blink
  STALE (no epoch for live.no_data_s)   yellow fast blink, others off

Safe action (outputs.safe_modes maps each state to OK / HOLD / SAFE; relay and servo positions per mode in
config). Uno line protocol, NMEA-style checksum (XOR of the characters between $ and *):
  Pi  -> Uno   $GG,<seq>,<MODE>,<servo_deg>,<relay 0|1>,<watchdog_ms>*CS     every outputs.uno.heartbeat_s
  Uno -> Pi    $GA,<seq>,<MODE>*CS                                         acknowledgement
The Uno goes to SAFE (relay off, servo at the SAFE angle) at power-up and whenever no valid line has
arrived for watchdog_ms: a hung Pi, a crashed process or a pulled cable all end in SAFE
(firmware/uno_safe_action/uno_safe_action.ino).

gpiozero is imported only when the LEDs are wanted (Raspberry Pi OS: apt install python3-gpiozero
python3-lgpio); on a laptop the LEDs are a no-op and say so once (event outputs_leds).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque

log = logging.getLogger("gnss_guardian.outputs")

LEDS = ("green", "yellow", "red", "blue")
MODES = ("OK", "HOLD", "SAFE")
STALE = "STALE"

ON = ((True, 1.0),)
OFF = ((False, 1.0),)


def blink(hz: float) -> tuple:
    return ((True, 0.5 / hz), (False, 0.5 / hz))


DOUBLE = ((True, 0.15), (False, 0.15), (True, 0.15), (False, 0.85))


def led_pattern(state: str | None, cause: str | None, jam_led: str = "blue") -> dict[str, tuple]:
    """{led: ((level, seconds), ...)} cycled from the moment the pattern starts. ``jam_led``
    (outputs.jam_led) is the LED that blinks for jamming: blue, or red on a 3-LED build (red steady =
    spoofing, red blinking = jamming)."""
    p = {k: OFF for k in LEDS}
    alarm = {"red": ON} if cause == "spoofing" else {jam_led: blink(2.0)} if cause == "jamming" else {}
    if state == "GNSS_OK":
        p["green"] = ON
    elif state in ("WARMING_UP", "SUSPECT", None):
        p["yellow"] = ON
    elif state == "SPOOFED":
        p["red"] = ON
    elif state == "JAMMED":
        p[jam_led] = blink(2.0)
    elif state in ("VISION_NAV", "RETURN_HOME"):
        p.update(alarm, green=blink(0.5))
    elif state == "HOME_REACHED":
        p.update(alarm, green=DOUBLE)
    elif state == STALE:
        p["yellow"] = blink(4.0)
    return p


def level_at(pattern: tuple, t: float) -> bool:
    period = sum(d for _, d in pattern)
    x = t % period if period > 0 else 0.0
    for lvl, d in pattern:
        if x < d:
            return lvl
        x -= d
    return pattern[-1][0]


def checksum(body: str) -> str:
    x = 0
    for ch in body:
        x ^= ord(ch)
    return f"{x:02X}"


def uno_line(seq: int, mode: str, servo_deg: int, relay: bool, watchdog_ms: int) -> bytes:
    body = f"GG,{seq},{mode},{int(servo_deg)},{1 if relay else 0},{int(watchdog_ms)}"
    return f"${body}*{checksum(body)}\n".encode("ascii")


def parse_uno(line: str) -> dict | None:
    """{'kind': 'GA'|'GR', ...} from a valid line the Uno sent, else None."""
    line = line.strip()
    if not line.startswith("$") or "*" not in line:
        return None
    body, cs = line[1:line.rindex("*")], line[line.rindex("*") + 1:]
    if checksum(body) != cs.upper()[:2]:
        return None
    f = body.split(",")
    if f[0] == "GA" and len(f) >= 3:
        return {"kind": "GA", "seq": int(f[1]) if f[1].isdigit() else None, "mode": f[2]}
    if f[0] == "GR":                                  # power-up banner
        return {"kind": "GR", "detail": ",".join(f[1:])}
    return None


# ---------------------------------------------------------------- backends
class NullLeds:
    name = "none"

    def set(self, led: str, on: bool) -> None:
        pass

    def close(self) -> None:
        pass


class FakeLeds:
    """Records every LED change (tests)."""
    name = "fake"

    def __init__(self):
        self.level = {k: False for k in LEDS}
        self.changes: list[tuple[str, bool]] = []

    def set(self, led, on):
        self.level[led] = on
        self.changes.append((led, on))

    def close(self):
        for k in LEDS:
            self.level[k] = False


class GpioLeds:
    name = "gpio"

    def __init__(self, pins: dict, active_high: bool = True):
        from gpiozero import LED                      # Raspberry Pi OS only; ImportError elsewhere
        bad = {k: p for k, p in pins.items() if p in (14, 15)}
        if bad:
            raise ValueError(f"LED pins {bad} are the L86 UART (GPIO14/15)")
        self.leds = {k: LED(pins[k], active_high=active_high, initial_value=False) for k in LEDS
                     if pins.get(k) is not None}           # pin null: that LED is not fitted

    def set(self, led, on):
        if led in self.leds:
            (self.leds[led].on if on else self.leds[led].off)()

    def close(self):
        for d in self.leds.values():
            d.off()
            d.close()


class GpioSafe:
    """Relay + servo straight on the Pi (fallback when there is no Uno; no watchdog)."""
    name = "gpio"

    def __init__(self, pins: dict):
        from gpiozero import AngularServo, OutputDevice
        self.relay = OutputDevice(pins["relay"], active_high=True, initial_value=False)
        self.servo = AngularServo(pins["servo"], min_angle=0, max_angle=180)

    def send(self, seq, mode, servo_deg, relay, watchdog_ms):
        (self.relay.on if relay else self.relay.off)()
        self.servo.angle = servo_deg

    def poll(self) -> list[dict]:
        return []

    def close(self):
        self.relay.off()
        self.relay.close()
        self.servo.close()


class UnoSafe:
    """The Uno over USB serial (/dev/ttyACM0). Opening the port resets the Uno (DTR): it ignores lines
    for its boot time and stays SAFE meanwhile, so the heartbeat simply keeps going."""
    name = "uno"

    def __init__(self, port: str, baud: int, serial_mod=None):
        if serial_mod is None:
            import serial as serial_mod
        self.ser = serial_mod.serial_for_url(port, baudrate=baud, timeout=0)
        self._buf = b""

    def send(self, seq, mode, servo_deg, relay, watchdog_ms):
        self.ser.write(uno_line(seq, mode, servo_deg, relay, watchdog_ms))

    def poll(self) -> list[dict]:
        self._buf += self.ser.read(4096) or b""      # timeout 0: what has arrived (in_waiting is 0/1 on socket://)
        *lines, self._buf = self._buf.split(b"\n")
        self._buf = self._buf[-256:]
        return [m for m in (parse_uno(x.decode("ascii", "replace")) for x in lines) if m]

    def close(self):
        self.ser.close()


# ---------------------------------------------------------------- controller
class Outputs:
    def __init__(self, cfg: dict, events: deque | None = None, leds=None, safe=None, safe_action: bool | None = None,
                 clock=time.monotonic, start: bool = True):
        o = cfg["outputs"]
        self.o, self.clock = o, clock
        self.events = events if events is not None else deque()
        self.stale_s = cfg["live"]["no_data_s"]
        self.safe_action = o["safe_action"] if safe_action is None else safe_action
        self._lock = threading.Lock()
        self._state: str | None = None
        self._cause: str | None = None
        self._last_update = clock()
        self._shown: tuple | None = None
        self._t0 = clock()
        self._level = {k: None for k in LEDS}
        self._seq = 0
        self._next_hb = 0.0
        self._sent_mode: str | None = None
        self._last_ack: float | None = None
        self._link_ok: bool | None = None
        self._stop = threading.Event()
        self.mode = "SAFE"
        self.leds = leds if leds is not None else self._open_leds()
        self.safe = safe if safe is not None else (self._open_safe() if self.safe_action else None)
        self.thread = None
        if start:
            self.thread = threading.Thread(target=self._run, daemon=True, name="outputs")
            self.thread.start()

    def _event(self, kind: str, **data) -> None:
        self.events.append((kind, data))

    def _open_leds(self):
        want = self.o["leds"]
        if want == "off":
            return NullLeds()
        try:
            leds = GpioLeds(self.o["pins"], self.o["active_high"])
            self._event("outputs_leds", backend="gpio", pins=self.o["pins"])
            return leds
        except Exception as e:                       # no gpiozero (laptop), no GPIO access, bad pins
            self._event("outputs_leds", backend="none", error=str(e))
            if want == "gpio":
                log.warning("status LEDs unavailable: %s", e)
            return NullLeds()

    def _open_safe(self):
        b = self.o["safe_backend"]
        try:
            if b == "uno":
                u = self.o["uno"]
                s = UnoSafe(u["port"], u["baud"])
            else:
                s = GpioSafe(self.o["safe_pins"])
            self._event("safe_action", enabled=True, backend=b, note="demo: ground stand-in only; guidance is advisory")
            log.warning("safe-action output ON (%s): ground stand-in only", b)
            return s
        except Exception as e:
            self._event("safe_action", enabled=False, backend=b, error=str(e))
            log.warning("safe-action output %s unavailable: %s", b, e)
            return None

    # ------------------------------------------------------------ serial-loop side
    def update(self, v: dict) -> None:
        """Newest verdict: stores state and cause only. Never blocks on hardware, never raises."""
        with self._lock:
            self._state, self._cause = v.get("state"), v.get("cause")
            self._last_update = self.clock()

    def wanted(self) -> tuple[str, str | None]:
        with self._lock:
            stale = self.clock() - self._last_update > self.stale_s
            return (STALE if stale else self._state), self._cause

    # ------------------------------------------------------------ worker
    def step(self) -> None:
        """One tick: LED levels for now, and the heartbeat when it is due (called by the thread; tests call it)."""
        state, cause = self.wanted()
        now = self.clock()
        if (state, cause) != self._shown:
            self._shown, self._t0 = (state, cause), now
            self._pattern = led_pattern(state, cause, self.o.get("jam_led") or "blue")
        for led in LEDS:
            on = level_at(self._pattern[led], now - self._t0)
            if on != self._level[led]:
                self._apply(lambda led=led, on=on: self.leds.set(led, on), "leds")
                self._level[led] = on
        self.mode = self.o["safe_modes"].get(state or "WARMING_UP", "SAFE")
        if self.safe is not None and (now >= self._next_hb or self.mode != self._sent_mode):   # a change goes at once
            self._next_hb = now + self.o["uno"]["heartbeat_s"]
            self._seq += 1
            seq, mode = self._seq, self.mode
            self._sent_mode = mode
            self._apply(lambda: self.safe.send(seq, mode, self.o["servo_deg"][mode], mode in self.o["relay_on"],
                                               int(self.o["uno"]["watchdog_s"] * 1000)), "safe")
            for m in (self._apply(self.safe.poll, "safe") if self.safe is not None else None) or []:
                if m["kind"] == "GA":
                    self._last_ack = now
                elif m["kind"] == "GR":
                    self._event("uno_boot", detail=m["detail"])
            ok = self._last_ack is not None and now - self._last_ack <= self.o["uno"]["watchdog_s"]
            if ok != self._link_ok:
                self._link_ok = ok
                self._event("uno_link", ok=ok, mode=mode)

    def _apply(self, fn, what: str):
        try:
            return fn()
        except Exception as e:
            self._event("outputs_error", output=what, error=repr(e))
            log.warning("%s output failed: %r", what, e)
            if what == "leds":
                self.leds = NullLeds()
            else:
                self.safe = None
            return None

    def _run(self):
        while not self._stop.wait(self.o["tick_s"]):
            try:
                self.step()
            except Exception as e:                   # the worker must outlive any output bug
                self._event("outputs_error", output="worker", error=repr(e))
                log.exception("outputs worker step failed")

    def close(self) -> None:
        self._stop.set()
        if self.thread is not None:
            self.thread.join(timeout=1.0)
        if self.safe is not None:
            self._apply(lambda: self.safe.send(self._seq + 1, "SAFE", self.o["servo_deg"]["SAFE"], False,
                                               int(self.o["uno"]["watchdog_s"] * 1000)), "safe")
            self._apply(self.safe.close, "safe")
        self._apply(self.leds.close, "leds")
