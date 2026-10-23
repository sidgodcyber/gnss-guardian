"""Command-line parsing: live --rate takes a number (1, "1", 5.0) and rejects unsupported rates."""
from unittest import mock

import pytest
from click.testing import CliRunner

from gnss_guardian.cli import cli


def live_rate(*args):
    with mock.patch("gnss_guardian.io.serial_live.run_live", return_value={"epochs": 0, "first_alarm": {}}) as m:
        r = CliRunner().invoke(cli, ["live", "--l86", "socket://127.0.0.1:1", "--no-camera", *args])
    return r, (m.call_args.kwargs["rate"] if m.called else None)


@pytest.mark.parametrize("args, hz", [((), 1), (("--rate", 1), 1), (("--rate", "1"), 1), (("--rate", "5"), 5),
                                      (("--rate", "10.0"), 10), (("--rate", 5.0), 5)])
def test_live_rate_accepts_numbers(args, hz):
    r, rate = live_rate(*args)
    assert r.exit_code == 0, r.output
    assert rate == hz and type(rate) is int


@pytest.mark.parametrize("value", ["2", "0", "fast"])
def test_live_rate_rejects_other_values(value):
    r, rate = live_rate("--rate", value)
    assert r.exit_code == 2 and rate is None and "--rate" in r.output
