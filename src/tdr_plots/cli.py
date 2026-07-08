import re
import time
from typing import Optional, Literal
import logging
import serial
import serial.tools.list_ports
import click
from pydantic import BaseModel
import pyvisa

from .tdr01_control.common import TraceSettings, TimingParams
from .tdr01_control.control import Device, take_trace, setup, set_timing
from .live_plot import run_monitor_plot

BAUDRATE = 115200
DEFAULT_TCP_PORT = 4000  # UART forwarding port, matches the bench TDR01 convention

_COM_PORT_RE = re.compile(r"^COM\d+$", re.IGNORECASE)


class Setup(BaseModel):
    baudrate: int = 115200
    spacing: float
    ramp_mode: str = "RAMP1"
    maxtime: float = 1e6
    sleep_time: float = 1e-3
    rc: Optional[float] = None
    m: Optional[float] = None


log_ = logging.getLogger("monitor_tdr")


def list_serial_ports():
    """List available COM ports in Windows and Linux"""
    ports = serial.tools.list_ports.comports()
    return sorted([port.device for port in ports if port.description], reverse=True)


def build_resource_name(device_str: Optional[str], tcp_port: int = DEFAULT_TCP_PORT) -> str:
    """Resolve --device into a pyvisa resource string, covering serial and IP.

    Accepts, in order:
      - None: auto-detect the first serial port.
      - A full pyvisa resource string (contains "::"), used as-is — e.g.
        "TCPIP0::192.168.1.100::4000::SOCKET" or "ASRL/dev/ttyUSB0::INSTR".
      - A serial port name ("COM3", "/dev/ttyUSB0", ...), wrapped as ASRL.
      - An IP address or hostname, optionally "host:port", wrapped as a
        TCPIP SOCKET resource (port defaults to tcp_port).
    """
    if device_str is None:
        com_ports = list_serial_ports()
        if len(com_ports) == 0:
            log_.error(
                "No com ports found or declared. Use --device to set a serial "
                "port or an IP address."
            )
            raise UserWarning("No com ports found or declared.")
        device_str = com_ports[0]

    if "::" in device_str:
        return device_str

    if device_str.startswith("/dev/") or _COM_PORT_RE.match(device_str):
        return f"ASRL{device_str}::INSTR"

    if "." in device_str or ":" in device_str:
        host, _, port = device_str.partition(":")
        return f"TCPIP0::{host}::{port or tcp_port}::SOCKET"

    return f"ASRL{device_str}::INSTR"


@click.option(
    "--device",
    "device_str",
    default=None,
    help=(
        "Serial port (e.g. COM3, /dev/ttyUSB0), an IP address/hostname "
        "(optionally host:port, e.g. 192.168.1.100:4000) for talking to the "
        "TDR01 over its UART-forwarding socket, or a full pyvisa resource "
        "string. Auto-detects the first serial port if omitted."
    ),
)
@click.option(
    "--tcp-port",
    "tcp_port",
    type=int,
    default=DEFAULT_TCP_PORT,
    help=f"TCP port to use when --device is a bare IP/hostname (default {DEFAULT_TCP_PORT}).",
)
@click.option("--maxtime", type=int, default=20000)
@click.option("--spacing", type=int, default=10)
@click.option("--ramp_mode", type=str, default="RAMP1")
@click.option("--start_time", type=float, default=100)
@click.option("--averages", type=int, default=2)
@click.option("--set-timing", "set_timing_flag", is_flag=True)
@click.option("--rc", type=float, default=0)
@click.option("--m", type=float, default=0)
@click.option("--a", type=float, default=60075)
@click.option("--dummy", is_flag=True)
@click.option(
    "--sleep", "sleep_time", type=float, default=2, help="Sleep time in between traces"
)
@click.command()
def cli_main(
    device_str,
    tcp_port,
    maxtime,
    spacing,
    ramp_mode,
    start_time,
    averages,
    set_timing_flag,
    rc,
    m,
    a,
    dummy,
    sleep_time,
):
    logging.basicConfig()
    logging.getLogger().setLevel(logging.INFO)
    log_.setLevel(logging.DEBUG)

    npoints = int(round(maxtime / spacing))

    settings = TraceSettings(
        spacing=spacing,
        ramp_mode=ramp_mode,
        i_start=int(round(start_time / spacing)),
        npoints=npoints,
        naverages=averages,
    )

    if dummy:
        run_monitor_plot(settings=settings, rxdac=None, device=None)
        return

    assert settings.npoints == npoints

    resource_name = build_resource_name(device_str, tcp_port)
    log_.info("Connecting to %s", resource_name)
    rm = pyvisa.ResourceManager("@py")
    resource = rm.open_resource(resource_name)
    with Device(baudrate=BAUDRATE, resource=resource) as device:
        device.setup()
        if set_timing_flag:
            timing_params = TimingParams(
                npoints=npoints, dt_ps=spacing, a=a, rc=rc, m=0
            )

            set_timing(device, timing_params)

        header = setup(
            device=device,
            settings=settings,
        )

        # if rc or m:
        #    set_timing = (rc is not None) or (m is not None)

        log_.info(f"header: {header}")
        # , points=settings.npoints)
        rxdac = device.query_ascii_values("RXDAC?")
        run_monitor_plot(settings=settings, rxdac=rxdac, device=device)


def main():
    try:
        cli_main()
    except serial.serialutil.SerialException as e:
        click.echo(f"Serial Exception: Is the port correct? {e}")
        pass
    except pyvisa.errors.VisaIOError as e:
        click.echo(f"VISA I/O Exception: Is the device address correct? {e}")


if __name__ == "__main__":
    main()
