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
| 2 | Vision odometry (optical flow, ORB keyframe terrain map), calibration, camera ↔ GNSS cross-check | **done** (synthetic ground only; real Pi camera walks pending) |
| 3 | State machine, fused position, return home | next |
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
On the Pi: `gnss-guardian live --l86 /dev/serial0 --out session/live [--camera picam] [--rate 5] [--l86-constellations gps,glonass,galileo] [--home lat,lon]`.
If the camera cannot be opened, live mode logs `camera_unavailable` and carries on GNSS-only.

Vision demo: render a downward-camera walk over synthetic ground with matching NMEA, then replay both.
A slow spoof (natural C/N0, invisible to GNSS-only checks) is caught by the camera:
```powershell
.venv\Scripts\python tools\synth_walk.py synth --walk loop:20:10:1.2 --still-s 40 --duration-s 120 --fps 20 --nmea --nmea-args "--spoof drift --spoof-cn0 natural --spoof-at 70 --drift-mps 0.5"
.venv\Scripts\python -m gnss_guardian.cli replay synth\nmea.nmea --video synth\video.avi --out session\synth
.venv\Scripts\python -m gnss_guardian.cli vision-bench                  # frame rate on this machine
```

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
- GNSS velocity disagreeing with camera velocity (`DetectionEngine.observe_vision()`, fed by Phase 2):
  mean difference over 3 s (fast drag-off) or 10 s (slow drag-off), with both velocities taken over the same interval

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
| `--spoof drift --spoof-cn0 natural` | none from GNSS alone; spoofing via the camera | 2.0 m/s: 2 s, 1.0 m/s: 3 s, 0.5 m/s: 9 s (rendered video) |

False alarms (ALARM epochs):

| Data | ALARM | Notes |
|---|---|---|
| Simulator walk, 30 min | 0 | also 0 SUSPECT; synthetic C/N0 |
| Phone proxy 18 Oct (5 min) | 0 | 4 SUSPECT during a real satellite/fix dropout in the log |
| Phone proxy 21 Oct (5 min) | 0 | 0 SUSPECT |
| Real L86 clean (30 min) | pending | test skips until `tests/fixtures/real_l86_clean.nmea` exists |
| Real L86 foil wrap | pending | must reach JAMMED ≤ 3 s after the C/N0 collapse; skips until recorded |

## What Phase 2 does (vision)

The camera points straight down from the rig, about 1 m up. Per frame (640×480 grey):

1. **Optical flow:** Shi–Tomasi corners and pyramidal Lucas–Kanade with a forward-backward check. LK
   starts from a constant-velocity prediction.
2. **Motion fit:** a RANSAC similarity fit gives rotation and translation. Pixels become metres via the
   camera height and focal length (`camera.hfov_deg` = 53.5° for the OV5647, refined by `calibrate`).
3. **Bad frames:** a failed frame (blur, a turn starting abruptly, a foot in view) does not lose its
   motion. The flow keeps its reference frame for up to 5 frames, and an ORB match is tried if LK
   still fails.
4. **Quality:** inliers × inlier ratio × fit. Below 0.3 a frame is `low_texture`; a long stretch is
   `vision_lost` and the track stops instead of inventing motion.

**Heading** is the sum of image rotations. It is initialised from the GNSS course on the first 2 m of
straight walking, then slowly corrected (τ = 20 s) on straight stretches while GNSS is trusted. This also
learns how the camera is turned on the pole. Keyframe matches correct it during vision navigation. No
IMU, no phone.

**Position** is the GNSS fix from 3 s ago plus the camera displacement since then, while GNSS is trusted.
A spoof caught within 3 s never becomes the anchor. When GNSS stops being trusted, it is dead reckoning
from the last trusted fix, corrected by keyframe matches.

**Terrain map:** while GNSS is trusted, an ORB keyframe is stored every 2 m with its GNSS position, plus
the home keyframe where home was set. During vision navigation the current frame is matched to nearby
keyframes every 5 frames. That gives an absolute position and heading, blended in at gain 0.5. Arrival
home is confirmed when the home keyframe matches within 1.5 m. Thumbnails go to
`session/<name>/keyframes/`.

**Calibrate:** walk a tape-measured straight 10 m at working height, then run
`gnss-guardian calibrate --video walk/video.avi --distance 10`. This writes `config/calibration.yaml`
(merged over the defaults).

### Measured (rendered ground: soil/gravel texture, 1.5° pole sway, 3 cm height bob, sensor noise)

| Test | Result |
|---|---|
| Straight 10 m, odometry only | 0.015 m drift (0.15 %); 0.03 m with 3° sway / 5 cm bob |
| 60 m loop with four corners, odometry only | 0.03–0.08 m closure, heading ≤ 0.3° (10–30 fps) |
| GNSS-denied lap with a 12 % calibration error | worst 1.44 m dead reckoning → 0.67 m with keyframe matches; home confirmed |
| Calibration from a 10 m walk (true focal 635 px, start 700 px) | fitted 634.8 px |
| Vision vs GNSS position while trusted | median 0.09 m, max 0.66 m (corners) |
| Frame rate, this laptop (i5-8365U), 640×480 | odometry 68–130 fps all cores, 48–57 fps one core; with ORB matching 55–84 fps |
| Frame rate, Pi 5 | **not measured**; expected ~25–50 fps at 640×480 and ~50–75 fps at `process_scale: 0.5`, from CPU benchmark ratios (~0.4–0.6× this laptop). Run `gnss-guardian vision-bench` on the Pi. |

## Limits (read before quoting results)

- **Vision numbers are from rendered ground, not footage.** Real ground adds lens distortion (the OV5647
  has visible barrel distortion), uneven surfaces and grass, pole tilt that does not average out, shadows
  and motion blur. Expect several times the rendered drift until the Pi camera walks are recorded and
  measured. Vision thresholds (velocity cross-check, quality) are tuned on synthetic walks only.
- **Flat ground at a fixed height is assumed.** Height comes from the config; tilting the pole biases the
  scale. A gimbal or the optional IMU would help.
- **Low texture** (smooth concrete, water, darkness) stops the vision track (`vision_lost`) rather than
  guessing. Daylight and a short exposure are needed against motion blur.

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

`tools/record_session.py` records the Pi camera and the L86 together on one clock (standalone:
`python3-picamera2`, `python3-opencv`, `python3-serial`). It writes `video.avi`, `frames.csv` and
`nmea.nmea`, which replay together with `gnss-guardian replay walk/nmea.nmea --video walk/video.avi`:
```bash
python3 record_session.py --out calib10m --duration-min 1      # tape-measured straight 10 m, stand 3 s at each end
python3 record_session.py --out walk01 --duration-min 5        # stand 90 s, then walk a loop back to the start
```

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
  vision/camera.py         Picamera2 (import-guarded) / USB / video file sources, one clock with GNSS
  vision/flow.py           Shi-Tomasi + pyramidal LK + RANSAC similarity, bridging, ORB fallback
  vision/odometry.py       camera track and heading, GNSS-course alignment
  vision/terrain.py        ORB keyframe map, keyframe and home matching
  vision/nav.py            VisionNav: anchoring to trusted GNSS, corrections, observe_vision feed
  vision/calibrate.py      focal length from a measured walk      vision/bench.py  frame-rate benchmark
  run.py session.py home.py status.py cli.py
tools/fake_l86.py          serial simulator with jam/spoof/walk profiles, --write for files
tools/synth_walk.py        rendered downward-camera walks (video + frames.csv + truth + matching NMEA)
tools/record_nmea.py       standalone NMEA recorder for the Pi
tools/record_session.py    standalone camera + NMEA recorder for the Pi
tools/make_proxy_fixture.py  phone log -> L86-style proxy NMEA (needs an IonWatch checkout; offline)
```

Session output: `session/<name>/session.json` (run info, home), `epochs.jsonl` (one verdict per epoch),
`events.jsonl` (level changes, home, serial, receiver, camera and keyframe events), `latest.json`,
`raw/`, `keyframes/`. With a camera, each epoch also carries a `vision` block (position at the epoch's
time, heading, quality, mode, keyframe match, home confirmation).

Pins: `pandas<3`, `numpy<2.5` (kept from IonWatch), `opencv-python-headless<5` (the Pi's apt OpenCV is 4.x).
