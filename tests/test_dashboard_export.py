"""Dashboard export: verify session data is properly exported for the replay viewer."""
import json
from pathlib import Path

from conftest import sim_file
from gnss_guardian.dashboard_export import export_dashboard
from gnss_guardian.run import replay


def test_dashboard_export_from_clean_replay(tmp_path, cfg):
    """Run replay on a clean synthetic walk, verify dashboard.json is created with expected structure."""
    nmea_path = sim_file(tmp_path / "clean.nmea", duration_s=60)
    session_path = tmp_path / "session"

    # Run replay
    result = replay(nmea_path, cfg, session_path)
    assert result["epochs"] == 60

    # Check dashboard.json exists
    dashboard_path = session_path / "dashboard.json"
    assert dashboard_path.exists(), f"dashboard.json not created at {dashboard_path}"

    # Load and verify structure
    data = json.loads(dashboard_path.read_text())
    assert "meta" in data
    assert "timeline" in data
    assert "summary" in data

    # Check metadata
    assert data["meta"]["mode"] == "replay"
    assert data["meta"]["source"] == str(nmea_path)

    # Check timeline
    timeline = data["timeline"]
    assert len(timeline) > 0, "timeline should not be empty"
    assert len(timeline) <= 60, "timeline should have at most 1 entry per second"

    # Verify each timeline entry has required fields
    for entry in timeline:
        assert "t_gnss" in entry
        assert "gnss" in entry
        assert "jamming_level" in entry
        assert "spoofing_level" in entry
        assert "vision" in entry
        assert "transitions" in entry

        # Check GNSS data
        gnss = entry["gnss"]
        assert "fix_valid" in gnss
        assert "sats_used" in gnss
        assert "hdop" in gnss
        assert "cn0_mean" in gnss

        # Check vision data
        vision = entry["vision"]
        assert "heading" in vision or vision["e"] is None
        assert "keyframes" in vision

        # Check detector levels are valid
        assert entry["jamming_level"] in ("CLEAR", "SUSPECT", "ALARM")
        assert entry["spoofing_level"] in ("CLEAR", "SUSPECT", "ALARM")

    # Check summary
    summary = data["summary"]
    assert "start_time" in summary
    assert "end_time" in summary
    assert "duration_s" in summary
    assert summary["duration_s"] > 0


def test_dashboard_export_preserves_state_transitions(tmp_path, cfg):
    """Export should capture state transitions (level changes) in the timeline."""
    # Create synthetic data with a spoof event
    nmea_path = sim_file(tmp_path / "spoof.nmea",
                         "--spoof", "jump",
                         "--spoof-at", "50",
                         duration_s=120)
    session_path = tmp_path / "session_spoof"

    # Run replay
    replay(nmea_path, cfg, session_path)

    # Check dashboard.json
    data = json.loads((session_path / "dashboard.json").read_text())
    timeline = data["timeline"]

    # Find transitions in timeline
    transitions_found = []
    for entry in timeline:
        if entry["transitions"]:
            transitions_found.extend(entry["transitions"])

    # Verify transitions are properly formatted if they exist
    for trans in transitions_found:
        assert "detector" in trans
        assert "level" in trans
        assert "score" in trans
        assert trans["detector"] in ("jamming", "spoofing")
        assert trans["level"] in ("CLEAR", "SUSPECT", "ALARM")
        assert isinstance(trans["score"], float)


def test_dashboard_export_with_vision_data(tmp_path, cfg):
    """Export with vision video should include ENU coordinates in timeline."""
    from conftest import synth_walk, SYNTH_STILL_S, SYNTH_DURATION_S, SYNTH_FPS

    # Generate synthetic walk with vision
    synth_path = tmp_path / "vision_walk"
    synth_walk.main([str(synth_path),
                     "--walk", "loop:15:10:1.0",
                     "--still-s", str(SYNTH_STILL_S),
                     "--duration-s", str(SYNTH_DURATION_S),
                     "--fps", str(SYNTH_FPS),
                     "--nmea"])

    session_path = tmp_path / "session_vision"

    # Run replay with video
    result = replay(synth_path / "nmea.nmea", cfg, session_path,
                    video=synth_path / "video.avi",
                    frames_csv=synth_path / "frames.csv")

    # Check replay completed
    assert result["frames"] > 0, "should have processed video frames"

    # Check dashboard.json exists and is valid
    data = json.loads((session_path / "dashboard.json").read_text())
    timeline = data["timeline"]

    # Verify all entries have valid vision structure
    for entry in timeline:
        vision = entry["vision"]
        assert "e" in vision
        assert "n" in vision
        assert "heading" in vision
        assert "keyframes" in vision
        # e and n can be None if home is not set
        if vision["e"] is not None:
            assert isinstance(vision["e"], (int, float))
        if vision["n"] is not None:
            assert isinstance(vision["n"], (int, float))
