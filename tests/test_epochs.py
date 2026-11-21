"""Epoch assembly: one epoch per fix cycle at 1 and 10 Hz, GSV carried forward, cut-off cycles."""
from conftest import SIM_FIXTURE, nmea, sim_file

from gnss_guardian.io.epochs import EpochBuilder, epochs_from_lines


def test_fixture_one_epoch_per_second_with_fix_fields(cfg):
    eps = epochs_from_lines(SIM_FIXTURE.read_text().splitlines(), cfg)
    assert len(eps) == 600
    assert all((b.t_gnss - a.t_gnss).total_seconds() == 1 for a, b in zip(eps, eps[1:]))
    e = eps[0]
    assert e.fix_valid and e.hdop == 0.9 and e.sats_used == 11 and e.alt_m == 500.0
    assert len(e.tracked()) == 15 and e.sats_new and e.mono is None      # plain NMEA: no host clock


def test_ten_hz_epochs_and_gsv_carried_forward(cfg, tmp_path):
    eps = epochs_from_lines(sim_file(tmp_path / "s.nmea", "--write-rate", "10", duration_s=5).read_text()
                            .splitlines(), cfg)
    assert len(eps) == 50
    assert {round((b.mono - a.mono), 1) for a, b in zip(eps, eps[1:])} == {0.1}
    # a cycle without GSV keeps the last snapshot, marked as not new, with its age
    b = EpochBuilder(cfg)
    out = []
    for k, gsv in ((0, True), (1, False)):
        t = f"14000{k}.000"
        host = f"2026-10-22T14:00:0{k}.300Z,"
        lines = [nmea(f"GPRMC,{t},A,2915.3360,N,07932.2200,E,0.02,0.00,221026,,,A"),
                 nmea(f"GPGGA,{t},2915.3360,N,07932.2200,E,1,04,1.0,500.0,M,-40.0,M,,")]
        if gsv:
            lines.append(nmea("GPGSV,1,1,04,05,60,045,40,12,45,120,38,19,30,200,35,29,25,010,33"))
        for line in lines:
            out += b.feed(host + line)
    out += b.flush()
    assert [e.sats_new for e in out] == [True, False]
    assert len(out[1].tracked()) == 4 and abs(out[1].sats_age_s - 1.0) < 1e-6


def test_cut_off_cycle_still_yields_an_epoch(cfg):
    lines = SIM_FIXTURE.read_text().splitlines()[:16]
    lines[12] = lines[12][:20]                       # the second cycle's first GPGSV cut in half
    b = EpochBuilder(cfg)
    eps = []
    for line in lines:
        eps += b.feed(line)
    eps += b.flush()
    assert len(eps) == 2 and eps[1].fix_valid
    c = b.parser.counts
    assert c["noise"] + c["bad_checksum"] == 1 and c["gsv_incomplete"] == 1
    assert len(eps[1].tracked()) == len(eps[0].tracked())     # GPS satellites carried from the last complete group
