"""NMEA parser: checksum, capture formats, GSV assembly, fix fields, no-fix sentences, antenna status."""
import math

from conftest import T0, nmea

from gnss_guardian.io import nmea as nm


def test_checksum_and_capture_formats():
    s = nmea("GPGGA,1,2,3")
    assert nm.checksum_ok(s) and not nm.checksum_ok("$GPGGA,1,2,3*00")
    assert nm.split_prefix(s) == ("plain", None, s)
    fmt, host, sent = nm.split_prefix(f"2026-10-22T14:00:00.412Z,{s}")
    assert fmt == "iso_prefix" and sent == s and host.microsecond == 412000 and host.tzinfo is not None
    fmt, host, _ = nm.split_prefix(f"1792500000.5 {s}")
    assert fmt == "epoch_prefix" and host.timestamp() == 1792500000.5
    assert nm.split_prefix("garbage")[0] is None
    assert nm.sentence_type(s) == "GGA" and nm.sentence_type(nmea("PMTK001,220,3")) == "PMTK001"


def test_gsv_groups_empty_snr_and_used_in_fix(cfg):
    p = nm.NmeaParser(cfg)
    lines = [nmea("GPRMC,140000.000,A,2915.3360,N,07932.2200,E,0.02,0.00,221026,,,A"),
             nmea("GPGSA,A,3,05,12,,,,,,,,,,,1.6,0.9,1.3"),
             nmea("GPGSV,2,1,05,05,60,045,40,12,45,120,38,19,30,200,35,25,70,300,"),
             nmea("GPGSV,2,2,05,29,25,010,33")]
    rows = []
    for line in lines:
        rows += p.feed(line)
    assert len(rows) == 5 and p.counts["gsv_groups"] == 1 and p.counts["gsv_incomplete"] == 0
    by = {r["prn_raw"]: r for r in rows}
    assert math.isnan(by[25]["cn0_dbhz"]) and by[5]["cn0_dbhz"] == 40
    assert by[5]["used_in_fix"] and by[12]["used_in_fix"] and not by[19]["used_in_fix"]
    assert by[5]["time"] == T0


def test_fix_fields_hemispheres_and_no_fix(cfg):
    p = nm.NmeaParser(cfg)
    for line in [nmea("GPRMC,140000.000,A,3352.1280,S,15112.5600,W,3.89,271.50,221026,,,A"),
                 nmea("GPGGA,140000.000,3352.1280,S,15112.5600,W,1,09,1.20,45.3,M,-40.0,M,,"),
                 nmea("GPGSA,A,3,05,12,19,25,,,,,,,,,2.0,1.2,1.6")]:
        p.feed(line)
    f = p.fix
    assert abs(f["lat"] - -(33 + 52.128 / 60)) < 1e-9 and abs(f["lon"] - -(151 + 12.56 / 60)) < 1e-9
    assert abs(f["sog_mps"] - 3.89 * 0.514444) < 1e-6 and f["cog_deg"] == 271.5
    assert f["quality"] == 1 and f["sats_used"] == 9 and f["hdop"] == 1.2 and f["alt_m"] == 45.3
    assert f["fix_mode"] == 3 and f["pdop"] == 2.0 and f["rmc_valid"]
    # MTK no-fix output: empty position fields, RMC V, GGA quality 0, GSA mode 1
    for line in [nmea("GPRMC,140001.000,V,,,,,0.00,0.00,221026,,,N"), nmea("GPGGA,140001.000,,,,,0,00,,,M,,M,,"),
                 nmea("GPGSA,A,1,,,,,,,,,,,,,,,")]:
        p.feed(line)
    assert not p.fix["rmc_valid"] and "lat" not in p.fix and p.fix["quality"] == 0 and p.fix["fix_mode"] == 1
    assert p.counts["rmc_no_fix"] == 1


def test_antenna_status_txt_and_pgtop(cfg):
    p = nm.NmeaParser(cfg)
    p.feed(nmea("GPTXT,01,01,02,ANTSTATUS=OK"))
    assert p.antenna == "OK"
    p.feed(nmea("GPTXT,01,01,02,ANTSTATUS=OPEN"))
    assert p.antenna == "OPEN"
    p.feed(nmea("PGTOP,11,1"))
    assert p.antenna == "SHORT"
