# Test fixtures

| File | Source | Used for |
|---|---|---|
| `l86_sim_10min.nmea` | Synthetic L86 NMEA from IonWatch (`tools/make_l86_fixture.py` there): GPS + GLONASS + Galileo GSV, fixed position, 1 Hz | the simulator's base sky; parser and epoch tests |
| `proxy_phone_2026_10_18.nmea` | Real iQOO GnssLogger log 2026-10-18 08:39–08:44 UTC (Haldwani, daytime), converted by `tools/make_proxy_fixture.py`: GPS L1 + GLONASS G1, C/N0 + 8 dB, phone's own fixes | false-alarm check on real C/N0 dynamics. **A proxy, not an L86** |
| `proxy_phone_2026_10_21.nmea` | Real GnssLogger log `gnss_log_2026_10_21_01_34_12` (2026-10-20 20:04–20:09 UTC), same conversion | same |
| `real_l86_clean.nmea` | *pending*: 30 min outdoors, open sky, `tools/record_nmea.py` on the Pi | zero false alarms on the real receiver |
| `real_l86_foil.nmea` | *pending*: 90 s clean, 60 s antenna wrapped in foil, unwrapped to the end | JAMMED within 3 s of the C/N0 collapse |

The 2026-10-20 18:58 IST phone log (3.3 h) was tried as a proxy and dropped: indoors, with only 71 s of fix in 40 min.
