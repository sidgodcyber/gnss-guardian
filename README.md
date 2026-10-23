# gnss-guardian

A Raspberry Pi edge module that notices, within a few seconds, when satellite navigation stops being
trustworthy (jamming, blockage or spoofing). It is built so a drone can switch to camera-based
navigation and get back home. Hackathon problem #143 (Open Innovation). The tested payload is a
handheld pole rig, not a flying drone.

**Runtime inputs are on-board only:** Quectel L86-M33 (GNSS, UART), Pi Camera Rev 1.3 (vision, Phase 2),
and an optional MPU6050 on I2C (auto-detected, off by default). No phone, no network: the module keeps
working with Wi-Fi off, and the dashboard (Phase 4) only views its output. A phone GnssLogger recording is
an offline reference only ("reference, not used for navigation"), compared after a field test (Phase 4).

| Phase | Scope | Status |
|---|---|---|
| 1 | Per-epoch GNSS health, jamming/spoofing scores with reasons and hysteresis, simulator profiles | **done** |
| 2 | Vision odometry (optical flow, ORB keyframe map) | next |
| 3 | State machine, fused position, return home | |
| 4 | Dashboard, AI second opinion, phone comparison, demo scripts | |
| 5 | Pi setup (camera, UART `dtparam=uart0=on`, I2C), systemd, field test kit | |

Forked in spirit from IonWatch. The NMEA parser, serial live loop, PMTK rate setup and serial simulator
were copied and extended; no git history was carried over.

## Quick start (Windows laptop, no hardware)

```powershell
python -m venv .venv; .venv\Scripts\python -m pip install -e ".[test]"
.venv\Scripts\python -m pytest -q
```

One-shot demo: render a simulated walk with a ramp jam and recovery, then replay it through the detector:
```powershell
.venv\Scripts\python tools\fake_l86.py --write sim_jam.nmea --walk loop:20:10:1.2 --jam ramp --jam-at 120 --recover-after 40 --duration-s 300
.venv\Scripts\python -m gnss_guardian.cli replay sim_jam.nmea --out session\jam
```
```
2026-10-22T14:01:02Z  baseline ready, detection active
2026-10-22T14:01:33Z  home set: 29.255690, 79.537158 (median of the first 30 s of trusted fixes)
2026-10-22T14:02:02Z  jamming  -> SUSPECT 0.64  C/N0 down >= 6 dB on 12/12 satellites; mean C/N0 drop 8.2 dB
2026-10-22T14:02:03Z  jamming  -> ALARM   0.70  C/N0 down >= 6 dB on 12/12 satellites; mean C/N0 drop 11.8 dB
2026-10-22T14:02:49Z  jamming  -> SUSPECT 0.00
2026-10-22T14:02:53Z  jamming  -> CLEAR   0.00
```

Live demo through the serial path (the simulator serves `socket://`, the Windows stand-in for `/dev/serial0`):
```powershell
.venv\Scripts\python tools\fake_l86.py --transport tcp --walk loop:20:10:1.2 --spoof jump --spoof-at 120   # prints PORT socket://127.0.0.1:NNNN
.venv\Scripts\python -m gnss_guardian.cli live --l86 socket://127.0.0.1:NNNN --out session\live
.venv\Scripts\python -m gnss_guardian.cli status --data session\live
```
On the Pi: `gnss-guardian live --l86 /dev/serial0 --out session/live [--rate 5] [--l86-constellations gps,glonass,galileo] [--home lat,lon]`.

## What Phase 1 detects

Every receiver fix (1 Hz default, 5/10 Hz with `--rate`) becomes an epoch. Features are computed against a
rolling 60 s baseline of **clean** epochs only. The baseline freezes while either detector is above CLEAR,
so a long attack cannot become the new normal.

**GNSS denied: jamming or blockage** (weighted sum, 0–1):

- fraction of judged satellites with C/N0 ≥ 6 dB below their own baseline, or lost
- mean C/N0 drop
- satellites lost
- HDOP rising against baseline
- fix lost (score ≥ 0.9 when the fix is lost during a broadband drop)

If the satellites still tracked keep their C/N0, losing the others is treated as a sky change or partial
blockage and the score is damped. A wall, or a spoofer's new sky, is not broadband denial.

**GNSS spoofing suspected** (noisy-OR, so one strong indicator is enough):

- implied speed between fixes, or reported speed, above `rig.max_speed_mps` (3 m/s for the rig)
- GNSS time jumping against the host monotonic clock
- altitude rate
- C/N0 suspiciously uniform and high across satellites
- tracked satellites never seen in the baseline window
- GNSS velocity disagreeing with camera velocity: the Phase 2 hook `DetectionEngine.observe_vision()`, mean over 3 s

Kinematic indicators are not blamed on spoofing while the fix is degraded by jamming.

Each detector moves through CLEAR → SUSPECT → ALARM with hysteresis:

- **ALARM** on 2 consecutive epochs ≥ 0.6, or 1 epoch ≥ 0.8.
- **Leaving ALARM** needs 5 s below 0.3.
- **Spoof latch:** a spoof alarm stays at least 30 s; Phase 3 releases it on vision agreement.

Every verdict lists its reasons. GNSS is "trusted" only when the baseline is warm, the fix valid and both
detectors are CLEAR. ANTSTATUS (`GPTXT`/`PGTOP`) is logged; OPEN/SHORT is reported as a hardware fault,
not as jamming. All thresholds are in [config/default.yaml](config/default.yaml).

### Measured (simulator, 1 Hz, walking rig; latency = profile start → first ALARM)

| Profile | Detector | Latency |
|---|---|---|
| `--jam sudden` (−25 dB) | jamming | 0 s |
| `--jam ramp` (4 dB/s) | jamming | 3 s (6 dB is crossed at +1.5 s); 1.8 s at 10 Hz |
| `--spoof jump` (500 m) | spoofing | 0 s |
| `--spoof drift` (1 m/s, spoofer's uniform sky) | spoofing | 1 s |
| `--spoof time` (+30 s) | spoofing | 0 s |
| `--spoof drift --spoof-cn0 natural` | none from GNSS; spoofing via the vision hook | ≤ 5 s (synthetic vision, 0.2 m/s noise) |

False alarms (ALARM epochs):

| Data | ALARM | Notes |
|---|---|---|
| Simulator walk, 30 min | 0 | also 0 SUSPECT; synthetic C/N0 |
| Phone proxy 18 Oct (5 min) | 0 | 4 SUSPECT during a real satellite/fix dropout in the log |
| Phone proxy 21 Oct (5 min) | 0 | 0 SUSPECT |
| Real L86 clean (30 min) | pending | test skips until `tests/fixtures/real_l86_clean.nmea` exists |
| Real L86 foil wrap | pending | must reach JAMMED ≤ 3 s after the C/N0 collapse; skips until recorded |

## Limits (read before quoting results)

- **Jamming vs blockage:** the L86 reports no AGC or noise floor, so RF jamming, foil, buildings and dense
  canopy are indistinguishable from NMEA. The detector says "GNSS denied: jamming or blockage". For
  navigation the response is the same.
- **Slow drift with natural C/N0:** a drag-off below the rig's speed, keeping the real satellite set and
  realistic C/N0, is invisible to GNSS-only checks. Only the camera cross-check (Phase 2) catches it. A
  spoofer that reproduces the visible PRNs at realistic, varied power would also defeat the
  uniformity and newcomer indicators.
- **Tuning so far:** thresholds are tuned on the simulator and on phone-derived proxies. Those have real
  C/N0 dynamics but come from a phone, not the L86. Real L86 recordings are being made; until those tests
  run, false-alarm rates on the L86 itself are unmeasured. The vision thresholds are set from synthetic
  data only.
- **Simulator model:** tracking floor 12 dB-Hz, re-acquisition at 20, fix needs 4 satellites ≥ 25 dB-Hz,
  3 s to re-fix. This is a simple model, not MediaTek's. 10 Hz behaviour is tested only in the simulator.
- **Start-up:** detection starts after 60 s of clean data with a fix, and the automatic home point is the
  median of the next 30 s. **Stand still for the first 90 s.** Use `--home lat,lon` to give a surveyed home.
- **Galileo:** `--l86-constellations ...,galileo` sends PMTK353 and confirms by GAGSV appearing. Whether
  the module's firmware accepts it is checked at run time (`galileo_confirmed` / `galileo_missing` events).
- Real RF jamming or spoofing transmitters are illegal to operate in India; all tests use shielding (foil)
  or simulation.

## Recording real data

`tools/record_nmea.py` is standalone (pyserial only), so it can be copied to the Pi without the repo:
```bash
python3 record_nmea.py --out real_l86_clean.nmea --duration-min 30    # in tmux; status line every 10 s
```
Lines carry a host-time prefix (`2026-10-23T08:15:02.123Z,$GPRMC,...`), so replay can check GNSS time
against the host clock. Live mode writes the same format to `session/<name>/raw/`.

## Layout

```
config/default.yaml        every threshold
gnss_guardian/
  io/nmea.py               NMEA parser (IonWatch, + fix fields and antenna status)
  io/epochs.py             lines -> one Epoch per fix cycle
  io/serial_live.py        live serial loop: reconnect, silence reopen, PMTK rate/constellations, raw log
  detect/features.py       per-epoch features, clean-only rolling baseline
  detect/jamming.py        denial score      detect/spoofing.py  spoofing score
  detect/hysteresis.py     CLEAR/SUSPECT/ALARM levels
  detect/engine.py         Epoch -> Verdict (JSON-safe)
  run.py session.py home.py status.py cli.py
tools/fake_l86.py          serial simulator with jam/spoof/walk profiles, --write for files
tools/record_nmea.py       standalone recorder for the Pi
tools/make_proxy_fixture.py  phone log -> L86-style proxy NMEA (needs an IonWatch checkout; offline)
```

Session output: `session/<name>/session.json` (run info, home), `epochs.jsonl` (one verdict per epoch),
`events.jsonl` (level changes, home, serial and receiver events), `latest.json`, `raw/`.

Pins: `pandas<3`, `numpy<2.5` (kept from IonWatch).
