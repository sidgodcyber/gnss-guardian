# gnss-guardian

A Raspberry Pi 4 edge module that notices, within a few seconds, when satellite navigation stops being
trustworthy (jamming, blockage or spoofing). It is built so a drone can switch to camera-based
navigation and get back home. Hackathon problem #143 (Open Innovation). The tested payload is a
handheld pole rig, not a flying drone.

**Runtime inputs are on-board only:** Quectel L86-M33 (GNSS, UART), Pi Camera Rev 1.3 (vision, Phase 2).
No phone, and nothing is sent anywhere: the module keeps working with Wi-Fi off. With `--overlay PORT` the Pi
also *serves* its camera overlay, a guidance page and `live.json` on the LAN so a laptop or phone can watch
(listening only; the dashboard views, it never feeds the pipeline). A phone GnssLogger recording is an
offline reference only ("reference, not used for navigation"), compared after a field test (Phase 4).

| Phase | Scope | Status |
|---|---|---|
| 1 | Per-epoch GNSS health, jamming/spoofing scores with reasons and hysteresis, simulator profiles | **done** |
| 2 | Vision odometry (optical flow, ORB keyframe terrain map), calibration, camera ↔ GNSS cross-check | **done** (synthetic ground only; real Pi camera walks pending) |
| 3 | State machine, fused position, return-home guidance, spoof latch release on vision agreement | **done** (synthetic walks only) |
| 4 | Dashboard, AI second opinion, phone comparison, demo scripts | replay + live dashboard, `tools/demo.py`, `field-demo` done |
| 5 | Pi 4 setup script (camera, UART `dtoverlay=disable-bt`), systemd, field test kit | manual steps below; [road demo](#field-demo-road) ready, not yet run outdoors |

Forked in spirit from IonWatch. The NMEA parser, serial live loop, PMTK rate setup and serial simulator
were copied and extended; no git history was carried over.

## Quick start (Windows laptop, no hardware)

```powershell
python -m venv .venv; .venv\Scripts\python -m pip install -e ".[test]"
.venv\Scripts\python -m pytest -q
```

One-shot demo: render a simulated walk with a ramp jam and recovery, then replay it through the detector:
```powershell
.venv\Scripts\python tools\fake_l86.py --write sim_jam.nmea --walk loop:20:10:1.2 --still-s 100 --jam ramp --jam-at 120 --recover-after 40 --duration-s 300
.venv\Scripts\python -m gnss_guardian.cli replay sim_jam.nmea --out session\jam
```
```
2026-10-22T14:01:02Z  baseline ready, detection active
2026-10-22T14:01:33Z  home set: 29.255600, 79.537000 (median of 30 s of trusted fixes, spread 0.0 m, HDOP 0.9, 11 sats)
2026-10-22T14:02:02Z  jamming  -> SUSPECT 0.64  C/N0 down >= 6 dB on 12/12 satellites; mean C/N0 drop 8.2 dB
2026-10-22T14:02:03Z  jamming  -> ALARM   0.70  C/N0 down >= 6 dB on 12/12 satellites; mean C/N0 drop 11.8 dB
2026-10-22T14:02:49Z  jamming  -> SUSPECT 0.00
2026-10-22T14:02:53Z  jamming  -> CLEAR   0.00
```

Every replay also writes `session/NAME/dashboard.json` for the offline replay viewer: `cd dashboard; npm ci; npm run dev`, then open http://127.0.0.1:3000 (see [dashboard/README.md](dashboard/README.md)).

Phase 3 demo in one command: renders an out-and-back walk, simulates a jam and a spoof near the far end,
replays both with the camera through the state machine (session/demo_jam, session/demo_spoof) and starts
the dashboard (about 3.5 min on this laptop before the dashboard starts; `--no-dashboard` to build only,
`--port N` if 3000 is taken, `--open` to open the browser). It shortens the warm-up to 20 s and the home
window to 5 s through `demo/demo.yaml`; all other thresholds are the defaults.
```powershell
.venv\Scripts\python tools\demo.py
```
```
demo_jam:   WARMING_UP -> GNSS_OK -> JAMMED -> VISION_NAV -> RETURN_HOME -> HOME_REACHED -> GNSS_OK
demo_spoof: WARMING_UP -> GNSS_OK -> SPOOFED -> VISION_NAV -> RETURN_HOME -> HOME_REACHED -> GNSS_OK
```

Live demo through the serial path (the simulator serves `socket://`, the Windows stand-in for `/dev/serial0`):
```powershell
.venv\Scripts\python tools\fake_l86.py --transport tcp --walk loop:20:10:1.2 --still-s 100 --spoof jump --spoof-at 120   # prints PORT socket://127.0.0.1:NNNN
.venv\Scripts\python -m gnss_guardian.cli live --l86 socket://127.0.0.1:NNNN --out session\live --overlay 8080 --demo-inject
.venv\Scripts\python -m gnss_guardian.cli status --data session\live
```
Then open http://127.0.0.1:8080/guide (guidance page with the demo-injection buttons) and http://127.0.0.1:8080/
(overlay; "no camera" here). On the Pi 4 (after [Raspberry Pi 4 setup](#raspberry-pi-4-setup)):
`gnss-guardian live --l86 /dev/serial0 --out session/live [--camera picam] [--rate 5] [--l86-constellations gps,glonass,galileo] [--home lat,lon] [--overlay 8080 [--overlay-record] [--demo-inject]]`,
or the whole road demo with `gnss-guardian field-demo` ([below](#field-demo-road)).
If the camera cannot be opened, live mode logs `camera_unavailable` and carries on GNSS-only; if the overlay
port cannot be opened it logs `overlay_unavailable` and carries on without the display.

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

**GNSS spoofing** (noisy-OR, so one strong indicator is enough; labelled "GNSS spoofing suspected" at
SUSPECT and "GNSS SPOOFING ALARM" at ALARM):

- implied speed between fixes, or reported speed, above `rig.max_speed_mps` (3 m/s for the rig)
- GNSS time jumping against the host monotonic clock
- altitude rate
- C/N0 suspiciously uniform and high across satellites
- tracked satellites never seen in the baseline window
- GNSS velocity disagreeing with camera velocity (`DetectionEngine.observe_vision()`, fed by Phase 2):
  mean difference over 3 s (fast drag-off) or 10 s (slow drag-off), with both velocities taken over the same interval
- GNSS displacement disagreeing with camera displacement over 30 s (`vision_pos`, 3–6 m ramp). It works
  before the camera heading is aligned (it then compares lengths only) and when the receiver reports
  speed 0 while its position wanders: 0.2 m/s of drift under a still camera is 6 m in 30 s

Kinematic indicators are not blamed on spoofing while the fix is degraded by jamming. A jump within 10 s
of jamming or a fix gap still alarms (jam-then-spoof is a real attack), with the reason
"jump after degraded fix: toward/away from the camera position (GNSS x m -> y m from it)". With fewer than
6 judged satellites (indoors) the jamming score is capped at SUSPECT unless the fix is lost with them.

Each detector moves through CLEAR → SUSPECT → ALARM with hysteresis:

- **ALARM** on 2 consecutive epochs ≥ 0.6, or 1 epoch ≥ 0.8.
- **Leaving ALARM** needs 5 s below 0.3.
- **Spoof latch:** a spoof alarm stays at least 30 s. With a camera, the Phase 3 state machine holds it
  until GNSS and the camera agree, and then releases it (see below).

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

Flow runs on a half-size image (`vision.process_scale: 0.5`; the Pi 4 reaches the camera's 30 fps at 0.5
and 15.7 fps at 1.0). On the Pi camera the shutter is capped (`camera.max_exposure_us: 3000`: 2.9 px of
blur at 1.5 m/s, 1 m up); libcamera's auto exposure has no plain limit, so a small controller fixes the
shutter at the cap and moves brightness into gain when auto exposure wants longer (`camera_exposure` events
say which mode and values are in use).

**Heading** is the sum of image rotations. It is initialised from the GNSS course on the first 2 m of
straight walking, then slowly corrected (τ = 20 s) on straight stretches while GNSS is trusted. This also
learns how the camera is turned on the pole. Keyframe matches correct it during vision navigation. No
IMU, no phone.

**Position** is the GNSS fix from 3 s ago plus the camera displacement since then, while GNSS is trusted.
A spoof caught within 3 s never becomes the anchor. When GNSS stops being trusted, it is dead reckoning
from the last trusted fix, corrected by keyframe matches. When home is set automatically the anchor is
home itself (the rig stands on it, whatever the newest fix says), and while the camera has moved less than
0.3 m the anchor is held: GNSS wander under a still camera does not move the vision position.

**Terrain map:** while GNSS is trusted, an ORB keyframe is stored every 2 m with its GNSS position, plus
the home keyframe where home was set (retried every frame until it has 120 ORB features, given up with a
`home_keyframe_missing` event once the camera has left the spot). During vision navigation the current
frame is matched to nearby keyframes every 5 frames. That gives an absolute position and heading, blended
in at gain 0.5. The home keyframe is matched at any distance with a stricter test (Lowe ratio 0.7, ≥ 60
inliers, inlier ratio ≥ 0.5): ground that repeats (tiles, crop rows, paving) fails it, so it cannot fake
home. A strict home match resets the vision position and heading to home. Thumbnails go to
`session/<name>/keyframes/`.

**Calibrate:** push the rig along a tape-measured straight 5 or 10 m at its working height. On the Pi:
`gnss-guardian calibrate --live --distance 5` (Enter at the start mark, push, Enter at the end mark), or
from a recording: `gnss-guardian calibrate --video walk/video.avi --distance 10`. Either writes
`config/calibration.yaml`, which load_config merges over the defaults.

### Measured (rendered ground: soil/gravel texture, 1.5° pole sway, 3 cm height bob, sensor noise)

| Test | Result |
|---|---|
| Straight 10 m, odometry only | 0.015 m drift (0.15 %); 0.03 m with 3° sway / 5 cm bob |
| 60 m loop with four corners, odometry only | 0.03–0.08 m closure, heading ≤ 0.3° (10–30 fps) |
| GNSS-denied lap with a 12 % calibration error | worst 1.44 m dead reckoning → 0.67 m with keyframe matches; home confirmed |
| Calibration from a 10 m walk (true focal 635 px, start 700 px) | fitted 634.8 px |
| Vision vs GNSS position while trusted (heading aligned) | median 0.09 m, max 0.71 m (corners) |
| Frame rate, this laptop (i5-8365U), 640×480 | odometry 68–130 fps all cores, 48–57 fps one core; with ORB matching 55–84 fps |
| Frame rate, Pi 4 (bench, indoors, GNSS live) | 15.7 fps at `process_scale` 1.0; 30.0 fps at 0.5 (capped by the camera) |
| Overlay cost, this laptop | 2.6–4.2 ms per 320×240 frame at 5 fps; vision rate unchanged within run-to-run noise (181–192 vs 199–203 fps at 0.5) |
| Overlay cost, Pi 4 | **not measured**: run `gnss-guardian vision-bench --scale 0.5 --overlay` on the Pi |

## What Phase 3 does (state, fused position, return home)

One state per GNSS epoch, from the detector levels and the camera track ([gnss_guardian/state.py](gnss_guardian/state.py),
thresholds in `state:` and `guidance:` of [config/default.yaml](config/default.yaml)):

```
WARMING_UP -> GNSS_OK <-> SUSPECT -> JAMMED | SPOOFED -> VISION_NAV -> RETURN_HOME <-> HOME_REACHED
                 ^                         |________________________________________________|
                 |___________________ release (jamming: detectors CLEAR; spoofing: agreement) |
```

- **JAMMED / SPOOFED** on a detector ALARM (spoofing wins; a spoof during a jam turns the cause to
  spoofing). From here the state leaves only through the release rule.
- **VISION_NAV** whenever the camera is tracking (dead reckoning from the last trusted fix, track not
  lost). The heading need not be aligned yet: a still rig's position is right in any heading, and guidance
  then says `heading_aligned: false` (the guidance page shows a compass bearing instead of a turn).
- **RETURN_HOME** after 2 s of a tracking camera. Guidance is **advisory output only**: bearing
  (degrees from true north) and distance to home from the fused position, and the next waypoint on the
  reverse breadcrumb path: the keyframes stored while GNSS was trusted, from the one nearest the current
  position back to home. The module never commands the aircraft.
- **HOME_REACHED** when the camera position is within 2 m of home and the home keyframe matched (strictly)
  within the last 3 s (radius only when there is no home keyframe). Leaving by more than 4 m resumes guidance.
  A held position (camera track lost) never counts as arrival.
- **Fused position:** GNSS while trusted; otherwise the camera's dead reckoning anchored at the last
  trusted fix; `vision_stale` (held, not extrapolated) when the camera track stops on low texture;
  `last_fix` (held) without a camera.
- **Release:** a jam ends when both detectors are CLEAR. A spoof ends only after GNSS and the camera agree
  for 10 s in a row: position within 3 m, velocity within 0.3 m/s (on an unaligned still rig: the 30 s
  displacement check within 3 m), spoofing score below 0.3, jamming CLEAR, fix valid. Until then GNSS stays untrusted even when the detector clears by itself,
  so a spoofer that turns quiet does not become the anchor. On release the detector's latch is
  dropped too. Without a camera the detector's 30 s latch decides.
- **Output:** each epoch in `epochs.jsonl` carries `state`, `cause`, `fused`, `guidance` and `latch`.
  `events.jsonl` gets `state` (with reasons), `guidance` (the route) and `waypoint` events. The
  dashboard and `gnss-guardian status` read them.

- **Home** (automatic) is the median of 30 s of consecutive trusted fixes once their median HDOP ≤ 2.5,
  median satellites used ≥ 6 and every fix lies within 4 m of the median. Otherwise the window slides on,
  `home_waiting` is logged and `status` says why ("home not set: fixes spread 7.5 m > 4 m ...").

Keyframes other than home are stored only once the heading is aligned to the GNSS course; before that the
track runs in an arbitrary direction, and one keyframe of a 12 m east walk sat 2 m north.

### Measured (rendered out-and-back walk, simulated GNSS; nothing here is field data)

| Test | Result |
|---|---|
| Clean loop with camera | GNSS_OK throughout, fused = GNSS |
| Jam (fix lost) 9 s into a 12 m out-and-back walk | JAMMED → VISION_NAV 1 s → RETURN_HOME 2 s later → HOME_REACHED in the epoch of the true arrival → GNSS_OK after recovery |
| Spoof: 500 m jump, natural C/N0 | same chain; GNSS kept untrusted while the detector had cleared itself and GNSS was still 500 m off; released 11 s after the spoof ended (10 s of agreement) |
| Camera position on the way back | max 0.44 m from truth (rendered ground, correct calibration) |
| Ground turns smooth while walking, GNSS jammed | rig walked 4.8 m, fused position moved 0.00 m (`vision_stale`); never guided home on a frozen track |
| Road demo: 30 m out, injected 1 m/s drift spoof, guided back (`tests/test_road_demo.py`) | SPOOFED 4 s after the injection (camera velocity check) → VISION_NAV → RETURN_HOME → HOME_REACHED in the epoch of the true arrival; return error median 0.35 m, max 0.51 m; latch released 17 s after the injection stopped (10 s for the velocity windows to clear, 10 s of agreement) |

## Pi 4 bench findings (indoors, stationary) and what changed

| Finding | Cause | Change (test in `tests/test_bench_findings.py`) |
|---|---|---|
| Jam on the bench stayed JAMMED; never VISION_NAV / RETURN_HOME / HOME_REACHED | the camera counted as healthy only once its heading was aligned to the GNSS course, which needs ≥ 2 m of straight walking with GNSS trusted; a still rig never aligns | VISION_NAV whenever the camera tracks; still rig now goes JAMMED → VISION_NAV → RETURN_HOME → HOME_REACHED |
| Right after home was set GNSS was 16–17 m from home; vision continued from that fix ("home 17 m at 227 deg (vision)" while at home) | home was the median of wandering indoor fixes; vision anchored on the newest (wandering) fix | home quality gate (HDOP, satellites, spread); vision anchored at home when home is set; anchor held while the camera is still |
| No "home keyframe stored" | cancelled because that 17 m vision position was "far from home" | home keyframe retried until 120 features while the camera stays on the spot; status shows waiting / stored / missing |
| GNSS drifted 8 → 27 m over 75 s with zero camera motion, unflagged | velocity check needs an aligned heading and the receiver reported speed ~0 | 30 s displacement check: 0.25 m/s quiet drift with a still camera → SUSPECT 19 s, ALARM 24 s after onset; clean still GNSS with 1 m wander noise and a clean walk: no alarm |
| Snap back 20 m after reacquisition: spoof ALARM 0.95 (implied 12.4 m/s) | real jump after a degraded fix | alarm kept (jam-then-spoof is real); reason "jump after degraded fix: toward the camera position (17.8 m → 0.0 m)" |
| One SUSPECT indoors with no cover (2/4 satellites) | 2 of 4 judged satellites fading | below 6 judged satellites the score is capped at SUSPECT unless the fix is lost; foil (fix lost) still reaches ALARM |
| Foil: SUSPECT then JAMMED in ~2 s; partial foil only SUSPECT. Camera on/off: C/N0 22.6 vs 22.5 dB-Hz | as designed | none |

Also measured on the bench: vision 15.7 fps at scale 1.0 and 30.0 fps at 0.5 (camera-capped) with GNSS live.

## Limits (read before quoting results)

- **Vision numbers are from rendered ground, not footage.** Real ground adds lens distortion (the OV5647
  has visible barrel distortion), uneven surfaces and grass, pole tilt that does not average out, shadows
  and motion blur. Expect several times the rendered drift until the Pi camera walks are recorded and
  measured. Vision thresholds (velocity cross-check, quality) are tuned on synthetic walks only.
- **Flat ground at a fixed height is assumed.** Height comes from the config; tilting the pole biases the
  scale. A gimbal would help.
- **Low texture** (smooth concrete, water, darkness) stops the vision track (`vision_lost`) rather than
  guessing. Daylight and a short exposure are needed against motion blur.
- **Phase 3 is tested on scripted walks only.** The synthetic walker returns along a pre-rendered path; it
  does not follow the guidance. The state timers, home radius and agreement thresholds are set from
  these walks. A spoofer that stays within 3 m and 0.3 m/s of the camera for 10 s would be released.
- **Not yet verified on the Pi 4 / outdoors:** the exposure cap (written against Picamera2's API, unit-tested
  only), the overlay's cost on the Pi, `calibrate --live`, the guidance page on a phone, and the whole road
  demo. The displacement check's 3 m onset is set against simulated 1 m wander; real outdoor L86 wander
  may need it raised.

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
  median of the next 30 s that pass the home gate. **Stand still for the first 90 s** (longer if status says
  "home not set"). Use `--home lat,lon` to give a surveyed home.
- **Galileo:** `--l86-constellations ...,galileo` sends PMTK353 and confirms by GAGSV appearing. Whether
  the module's firmware accepts it is checked at run time (`galileo_confirmed` / `galileo_missing` events).
- Real RF jamming or spoofing transmitters are illegal to operate in India; all tests use shielding (foil)
  or simulation.

## Raspberry Pi 4 setup

The module runs on a Raspberry Pi 4 (64-bit Raspberry Pi OS Bookworm). Wiring is the same 40-pin header
as any Pi: L86 VCC → pin 1 (3.3 V), GND → pin 6, L86 TX → pin 10 (GPIO15, RXD), L86 RX → pin 8 (GPIO14,
TXD).

1. **UART.** On the Pi 4 the full UART (`ttyAMA0`) drives Bluetooth, and `serial0` points at the mini UART
   (`ttyS0`), whose baud rate follows the core clock. Give the full UART to the GPIO pins:
   `sudo raspi-config` → Interface Options → Serial Port → login shell **No**, hardware **Yes**. Then add to
   `/boot/firmware/config.txt` (`/boot/config.txt` on Bullseye):
   ```
   enable_uart=1
   dtoverlay=disable-bt
   ```
   Run `sudo systemctl disable hciuart`, then reboot. `ls -l /dev/serial0` must show `-> ttyAMA0`, and
   `timeout 10 cat /dev/serial0` must print NMEA.
2. **Camera.** The Pi Camera Rev 1.3 connects with its standard 15-pin ribbon straight into the Pi 4's
   CAMERA port (between HDMI and the audio jack): contacts toward the HDMI ports, latch closed. No adapter
   cable is needed. `rpicam-hello --list-cameras` must list `ov5647`; if not, set `camera_auto_detect=0`
   and `dtoverlay=ov5647` in `config.txt` and reseat the ribbon.
3. **Software.** `sudo apt install -y python3-picamera2 python3-opencv`, then
   `python3 -m venv --system-site-packages .venv` (so Picamera2 is visible) and `pip install -e .`.
4. **Frame rate.** Measured on the bench: 15.7 fps at scale 1.0, 30.0 fps (camera-capped) at 0.5, the
   default. Still to measure: `gnss-guardian vision-bench --scale 0.5 --overlay` (cost of the overlay).
5. **Power.** 5 V 3 A USB-C; `vcgencmd get_throttled` must read `0x0` with the camera and L86 running.

## Field demo (road)

A real ground demo on a road or parking lot. A person pushes a trolley carrying the Pi 4, L86 and Pi camera.
The module detects a spoof (injected in software) or a foil-covered antenna, switches to camera navigation
and guides the person back to the start. Everything is visible live on a laptop or phone browser.

**Rig.** Camera pointing straight down, lens about 1 m above the ground (set `camera.height_m` to the
measured height), **top of the image toward the push direction** (the guidance arrow is relative to the
camera's forward axis). L86 antenna on top with a clear sky view, away from the Pi. 5 V 3 A power bank. Pi
and phone/laptop on the same Wi-Fi (a phone hotspot works).

**Once per rig height:** calibrate on the road: mark 5 m with a tape, then
```bash
gnss-guardian calibrate --live --distance 5        # Enter at the start mark, push straight, Enter at the end
```

**Run:**
```bash
gnss-guardian field-demo --out session/road1       # = live --camera (config) --overlay 8080 --overlay-record --demo-inject
```
It prints the URLs (the Pi's LAN address):

| URL | For |
|---|---|
| `http://PI:8080/guide` | the person pushing (phone): arrow, distance, state, HOME REACHED, demo-injection buttons |
| `http://PI:8080/` | camera overlay (MJPEG): corners, flow, state, source, metres from home, keyframes, HOME MATCH, inset map |
| dashboard **Live** with `PI:8080` | everything else on the laptop (`cd dashboard; npm run dev`) |

**Sequence.**
1. Stand still on the start point until the guidance page shows GNSS_OK, home set and "home keyframe
   stored" (about 90 s; status says why if home waits).
2. Walk out 20–40 m with GNSS trusted (the first straight 2 m aligns the camera heading; keyframes every 2 m).
3. Spoof: on the guidance page press **Drift** (e.g. 0.5–1 m/s at 90°) or **Jump** (e.g. 30 m), or cover
   the antenna with foil (real blockage → JAMMED).
4. The module goes SPOOFED (or JAMMED) → VISION_NAV → RETURN_HOME; follow the arrow back.
5. HOME_REACHED when the home keyframe matches. Press **Stop injection**: GNSS returns only after it agrees
   with the camera for 10 s (the latch).
6. Afterwards: `gnss-guardian replay session/road1` writes the dashboard file into the session folder (with
   the recorded overlay as video); it then appears in the dashboard's session list.

**Real vs injected.**
- *Real:* the satellites, C/N0, HDOP, time and fix from the L86; the camera, odometry, keyframes, home match;
  the detector, state machine and guidance; a foil cover.
- *Injected (demo only):* the reported position (and, for a drift, the reported speed/course) is rewritten
  in software before the detector, which is not told. `raw/` keeps the NMEA exactly as received,
  `detector_input/` what the detector saw; every start/stop is a `demo_inject` event, and the overlay,
  guidance page, dashboard, `live.json` and `status` show **DEMO INJECTION ACTIVE** while it runs. No RF is
  transmitted.

## Recording real data

`tools/record_nmea.py` is standalone (pyserial only), so it can be copied to the Pi 4 without the repo:
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
  io/serial_live.py        live serial loop: reconnect, silence reopen, PMTK rate/constellations, raw log,
                           camera thread, display server, demo injection
  detect/features.py       per-epoch features, clean-only rolling baseline
  detect/jamming.py        denial score      detect/spoofing.py  spoofing score
  detect/hysteresis.py     CLEAR/SUSPECT/ALARM levels
  detect/engine.py         Epoch -> Verdict (JSON-safe)
  vision/camera.py         Picamera2 (import-guarded) / USB / video file sources, one clock with GNSS
  vision/flow.py           Shi-Tomasi + pyramidal LK + RANSAC similarity, bridging, ORB fallback
  vision/odometry.py       camera track and heading, GNSS-course alignment
  vision/terrain.py        ORB keyframe map, keyframe and home matching
  vision/nav.py            VisionNav: anchoring to trusted GNSS, corrections, observe_vision feed, breadcrumbs
  state.py                 Phase 3: state machine, fused position, return-home guidance, spoof latch release
  vision/calibrate.py      focal length from a measured walk      vision/bench.py  frame-rate benchmark
  livehub.py               live view (live.json) shared by the serial loop, overlay and display server
  overlay.py               camera overlay frames (own thread), MJPEG source, overlay.mp4
  web/server.py            display server: overlay stream, /guide, /live.json, /inject/*   web/pages.py  guide page
  inject.py                demo spoof injection into the NMEA before the detector (field demo only)
  run.py session.py home.py status.py cli.py
tools/fake_l86.py          serial simulator with jam/spoof/walk profiles, --write for files
tools/synth_walk.py        rendered downward-camera walks (video + frames.csv + truth + matching NMEA)
tools/demo.py              jam + spoof demo sessions with the camera, then the dashboard, in one command
tools/record_nmea.py       standalone NMEA recorder for the Pi
tools/record_session.py    standalone camera + NMEA recorder for the Pi
tools/make_proxy_fixture.py  phone log -> L86-style proxy NMEA (needs an IonWatch checkout; offline)
```

Session output: `session/<name>/session.json` (run info, home), `epochs.jsonl` (one verdict per epoch),
`events.jsonl` (level and state changes, return-home route and waypoints, home, serial, receiver, camera,
keyframe and demo_inject events), `latest.json`, `raw/`, `keyframes/`; live mode adds `live.json` (every
epoch), `detector_input/` (with `--demo-inject`) and `overlay.mp4` + `overlay_frames.csv` (with
`--overlay-record`). Each epoch carries `state`, `cause`, `fused`,
`guidance` and `latch`. With a camera it also carries a `vision` block (position at the epoch's time,
heading, quality, mode, keyframe match, home confirmation).

Pins: `pandas<3`, `numpy<2.5` (kept from IonWatch), `opencv-python-headless<5` (the Pi's apt OpenCV is 4.x).
