"""gnss-guardian command-line entry point.

    python -m gnss_guardian.cli live --l86 /dev/serial0 --out session/live [--rate 1|5|10]
                                     [--l86-constellations gps,glonass,galileo] [--home lat,lon]
    python -m gnss_guardian.cli replay FILE.nmea --out session/NAME [--speed 0] [--home lat,lon]
    python -m gnss_guardian.cli status --data session/live
"""
from __future__ import annotations

import sys

import click

from gnss_guardian.config import load_config
from gnss_guardian.home import parse_home


def _home(value):
    try:
        return parse_home(value)
    except ValueError as e:
        raise click.BadParameter(str(e)) from e


@click.group()
def cli():
    """GNSS jamming/spoofing detection (Phase 1 of gnss-guardian)."""


@cli.command()
@click.option("--l86", "port", required=True,
              help="Serial port (/dev/serial0, COM3) or pyserial URL (socket://127.0.0.1:5000).")
@click.option("--baud", default=9600, show_default=True, type=int)
@click.option("--rate", default=1, show_default=True, type=click.Choice(["1", "5", "10"]),
              help="L86 fix rate in Hz (5/10 switch the module to 115200 baud).")
@click.option("--l86-constellations", default=None,
              help="Send PMTK353, e.g. gps,glonass,galileo (verified by GAGSV appearing).")
@click.option("--out", default="session/live", show_default=True, type=click.Path(file_okay=False))
@click.option("--home", default=None, help="Home point lat,lon (default: set after the first trusted fixes).")
@click.option("--config", "config_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--clock-scale", default=1.0, show_default=True, type=float,
              help="Only with a fast simulator: simulated seconds per wall second.")
@click.option("--duration-s", default=None, type=float, help="Stop after this many wall seconds.")
def live(port, baud, rate, l86_constellations, out, home, config_path, clock_scale, duration_s):
    """Read the L86 live and run detection every fix."""
    from gnss_guardian.io.serial_live import run_live
    cfg = load_config(config_path)
    names = [c.strip().lower() for c in l86_constellations.split(",")] if l86_constellations else None
    s = run_live(port, baud, out, cfg, rate=int(rate), constellations=names, clock_scale=clock_scale,
                 duration_s=duration_s, home=_home(home))
    click.echo(f"epochs {s['epochs']}, first alarms {s['first_alarm'] or '-'}")


@cli.command()
@click.argument("nmea_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--out", required=True, type=click.Path(file_okay=False))
@click.option("--speed", default=0.0, show_default=True, type=float, help="0 = as fast as possible, N = N x real time.")
@click.option("--home", default=None, help="Home point lat,lon (default: set after the first trusted fixes).")
@click.option("--config", "config_path", type=click.Path(exists=True, dir_okay=False))
def replay(nmea_file, out, speed, home, config_path):
    """Run detection over recorded NMEA and print the event timeline."""
    from gnss_guardian.run import replay as run_replay
    cfg = load_config(config_path)
    s = run_replay(nmea_file, cfg, out, speed=speed, home=_home(home), echo=click.echo)
    click.echo(f"epochs {s['epochs']} | jamming ALARM {s['jamming_alarm_epochs']} / SUSPECT "
               f"{s['jamming_suspect_epochs']} | spoofing ALARM {s['spoofing_alarm_epochs']} / SUSPECT "
               f"{s['spoofing_suspect_epochs']} | first alarms {s['first_alarm'] or '-'}")
    click.echo(f"session: {out}")


@cli.command()
@click.option("--data", "folder", default="session/live", show_default=True, type=click.Path(file_okay=False))
@click.option("--every", default=1.0, show_default=True, type=float)
@click.option("--once", is_flag=True)
def status(folder, every, once):
    """One status line per second from the session's latest.json."""
    from gnss_guardian import status as st
    cfg = load_config()
    if once:
        line, stale = st.status_line(folder, cfg["display_tz"])
        click.echo(line)
        sys.exit(1 if stale else 0)
    try:
        st.watch(folder, every, echo=click.echo, tz=cfg["display_tz"])
    except KeyboardInterrupt:
        pass


def main():
    cli()


if __name__ == "__main__":
    main()
