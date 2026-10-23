# GNSS Guardian Replay Viewer

The replay viewer is a web-based dashboard for offline analysis of GNSS Guardian recording sessions. It shows jamming/spoofing detection events, GNSS signal health, vision odometry track, and state transitions.

## Quick Start

### 1. Record or Generate a Session

#### Option A: Run a synthetic demo (no hardware required)
```powershell
python tools/synth_walk.py demo --nmea --duration-s 60
python -m gnss_guardian.cli replay demo/nmea.nmea --out session/demo --video demo/video.avi
```

#### Option B: Replay an existing NMEA recording
```powershell
python -m gnss_guardian.cli replay recording.nmea --out session/my_recording --video camera.avi
```

The replay command automatically exports `session/<name>/dashboard.json` for the viewer.

### 2. Open the Replay Viewer

Open `dashboard/replay.html` in your web browser. This works fully offline.

### 3. Load a Session

1. Click **Load Session** and select the session folder (e.g., `session/demo/`)
2. The viewer loads `dashboard.json` and displays:
   - **Status cards**: GNSS validity, detection levels, satellite count, signal quality
   - **Timeline**: Interactive scrubber to move through the session
   - **Charts**: Detection scores, C/N0 (signal level), and HDOP over time
   - **Tactical Map**: GNSS and vision tracks relative to home
   - **State Log**: All level transitions (jamming/spoofing changes) over time

### 4. Controls

- **Play/Pause**: Animate through the session in real time
- **Rewind**: Jump back to the start
- **Speed**: 0.5×, 1×, or 2× playback speed
- **Scrubber**: Drag to jump to any point in the timeline

## Data Fields

The dashboard displays:

- **GNSS Status**: `Valid` or `Invalid` (fix_valid)
- **Satellites Used**: Count of satellites in the position solution
- **HDOP**: Horizontal Dilution of Precision (metres)
- **Mean C/N0**: Average signal strength across satellites (dB-Hz)
- **Jamming/Spoofing Levels**: `CLEAR` (green), `SUSPECT` (orange), `ALARM` (red)
- **Vision Track**: Local Easting/Northing position relative to home
- **Heading**: Camera heading (degrees)

## Exported Data Format

Each session exports a `dashboard.json` file with structure:

```json
{
  "meta": {
    "source": "demo/nmea.nmea",
    "mode": "replay",
    "created_utc": "2026-10-23T18:58:26.091Z",
    "home": null,
    "node_id": "guardian-01"
  },
  "timeline": [
    {
      "t_gnss": "2026-10-22T14:00:00",
      "gnss": {
        "fix_valid": true,
        "sats_used": 11,
        "hdop": 0.9,
        "cn0_mean": 35.1,
        "lat": 29.2556,
        "lon": 79.537
      },
      "jamming_level": "CLEAR",
      "jamming_score": 0.0,
      "spoofing_level": "CLEAR",
      "spoofing_score": 0.0,
      "vision": {
        "e": null,
        "n": null,
        "heading": 360.0,
        "keyframes": 0
      },
      "transitions": []
    }
  ],
  "summary": {
    "start_time": "2026-10-22T14:00:00",
    "end_time": "2026-10-22T14:02:59",
    "duration_s": 180,
    "max_hdop": 1.2,
    "max_cn0_mean": 38.5
  }
}
```

## Tabs

### Overview
- Detection score timeline (jamming and spoofing)
- Signal strength (mean C/N0) over time

### Tactical Map
- Home point (green marker)
- GNSS track (green line, or magenta if spoofing alarm)
- Vision track (blue marker when available)
- Grid overlay (5 m squares)

### GNSS
- HDOP timeline
- Shows signal geometry quality over time

### Detections
- List of recent state transitions
- Detector, level, score, and reasons for each event

## Troubleshooting

**"dashboard.json not found in session folder"**
- Ensure you ran `gnss-guardian replay` (not just `live`). The replay command exports the dashboard data.
- Or manually export existing session data:
  ```powershell
  python -c "from gnss_guardian.dashboard_export import write_dashboard_json; write_dashboard_json('session/my_session')"
  ```

**Charts not updating**
- Ensure JavaScript is enabled in your browser
- Try a modern browser (Chrome, Firefox, Safari, Edge)

**Vision track is empty**
- Vision data requires:
  1. A camera video file (`--video video.avi`)
  2. Frame timestamps or start time
  3. A home point to be set (automatically after 30 s of clean GNSS, or via `--home lat,lon`)

## Testing a Session with Known Events

### Synthetic Jamming
```powershell
python tools/fake_l86.py --write jam.nmea --walk loop:20:10:1.2 --jam ramp --jam-at 120 --recover-after 40 --duration-s 300
python -m gnss_guardian.cli replay jam.nmea --out session/jam
```
Look for jamming level changes around the 2 minute mark in the detection timeline.

### Synthetic Spoofing
```powershell
python tools/fake_l86.py --write spoof.nmea --walk loop:20:10:1.2 --spoof jump --spoof-at 120 --duration-s 300
python -m gnss_guardian.cli replay spoof.nmea --out session/spoof
```
Look for spoofing level changes in the detection timeline.

## Notes

- Positions are displayed in **local metres relative to home**, not latitude/longitude
- The viewer is **fully offline**: no data is sent to external services
- Detection thresholds and hysteresis are configured in `config/default.yaml`
- Vision odometry requires a calibrated camera (see `calibrate` command)
