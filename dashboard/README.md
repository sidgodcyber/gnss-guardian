# GNSS Guardian replay viewer

Offline viewer for session folders written by `gnss-guardian replay`. Based on the AeroShield AG
dashboard (React + Vite + Tailwind); it reads files only: no network, no telemetry link, no API keys.

## Run

```powershell
# 1. Export a session (writes session/NAME/dashboard.json, plus dashboard_video.webm with a video)
.venv\Scripts\python -m gnss_guardian.cli replay RECORDING_FOLDER --out session\NAME
#    RECORDING_FOLDER: nmea.nmea or gnss.nmea, optional video.avi or camera.avi + frames.csv
#    (tools/record_session.py and the standalone Pi recorders both work). A single .nmea file works too.

# 2. Start the viewer (first time: npm ci)
cd dashboard
npm run dev          # http://127.0.0.1:3000 - lists every session/<name> that has a dashboard.json
```

`$env:GG_SESSIONS="D:\path\to\sessions"; npm run dev` serves another folder. Without the dev server
(`npm run build`, then open `dist/` through any static server) use **Open folder…** and pick a
`session/<name>` folder; the files stay in the browser.

Deep links (also used for screenshots): `?session=NAME&tab=replay|logs&t=SECONDS`.

## What the panels show

| Panel | Source in dashboard.json |
|---|---|
| State ribbon, status cards | `timeline[].state` (from the pipeline's state machine), GNSS fix / sats / HDOP / mean C/N0, detector levels, vision mode, `fused` home distance and bearing |
| Detection panel | `config` (indicator spans, weights, hysteresis from `config/default.yaml`), `timeline[].jamming/spoofing.parts`, `transitions` (scrubber markers) |
| Tactical Map | `timeline[].gnss.e/n` (green), `timeline[].vision.e/n` (blue), `fused` (ring), `routes` (dashed, while guiding) and the arrow to `guidance.waypoint`, `keyframes`; metres from home, no lat/lon |
| Down-Cam VIO | `dashboard_video.webm`, synced through `timeline[].frame` |
| Sensor Fusion / Integrity Monitor | `timeline[].gnss`, `timeline[].vision`, `timeline[].features`, `fused`, `guidance`, `latch` (spoof-latch agreement progress) |
| Flight Logs | `transitions`, `detector_levels`, `events` |

The viewer works nothing out itself: the state, fused position, guidance and spoof-latch progress are
what the pipeline's state machine (`gnss_guardian/state.py`) wrote to `epochs.jsonl`. Guidance is advisory,
and the module never commands the aircraft. Sessions exported before Phase 3 (format 1) are refused with
a message to run `gnss-guardian replay` again. `python tools/demo.py` builds `demo_jam` and `demo_spoof`
and starts the viewer on them.
