from typing import Optional
import logging
import serial
import serial.tools.list_ports
import click
from tdr01_control.common import TraceSettings, RampModel
from tdr01_control.control import Device, take_trace

from tdr_plots.live_plot import run_monitor_plot
from pydantic import BaseModel


class Setup(BaseModel):
    baudrate: int = 115200
    spacing: float
    ramp_mode: int = 1
    maxtime: float = 1e6
    sleep_time: float = 1e-3
    rc: Optional[float] = None
    m: Optional[float] = None


log_ = logging.getLogger("monitor_tdr")


def setup(device, settings: TraceSettings, set_timing: bool = False):
    npoints = settings.npoints
    naverages = settings.naverages
    i_start = settings.i_start
    vbtx = settings.vbtx
    ramp_mode = settings.ramp_mode
    ramp_model = settings.ramp_model
    spacing = settings.spacing
    i_start = settings.i_start

    assert ramp_model.a > 10

    timing_params = f"{ramp_model.a} {ramp_model.rc} 0 0"
    settings_list = [
        ("E", 0),
        ("POINTS", npoints),
        ("RES", spacing),
        ("ISTART", i_start),
        ("AVG", naverages),
        ("VTX", vbtx),
        ("RAMP", ramp_mode),
    ]
    if set_timing:
        settings_list.append(("TIMING", timing_params))

    queries = (
        "RES?",
        "ISTART?",
        "POINTS?",
        "TIMING?",
        "AVG?",
        "VTX?",
        "RAMP?",
        "*IDN?",
    )

    header = {}

    device.flush()
    for key, value in settings_list:
        command = f"{key} {value}\n"
        device.write(command)
        device.flush()
        msg = f"{command}"
        log_.debug(msg)

    for key in queries:
        header[key] = device.query(key).strip()
        device.flush()

    log_.info("settings: %s\nqueries %s", str(settings_list), str(header))
    return header


def list_serial_ports():
    """List available COM ports in Windows and Linux"""
    ports = serial.tools.list_ports.comports()
    return sorted([port.device for port in ports if port.description], reverse=True)


@click.option("--device", "device_str", default=None)
@click.option("--maxtime", type=int, default=20000)
@click.option("--spacing", type=int, default=10)
@click.option("--ramp_mode", type=int, default=1)
@click.option("--start_time", type=float, default=0)
@click.option("--rc", type=float, default=None)
@click.option("--m", type=float, default=None)
@click.option(
    "--sleep", "sleep_time", type=float, default=2, help="Sleep time in between traces"
)
@click.command()
def cli_main(device_str, maxtime, spacing, ramp_mode, start_time, rc, m, sleep_time):
    logging.basicConfig()
    logging.getLogger().setLevel(logging.INFO)
    log_.setLevel(logging.DEBUG)

    if device_str is None:
        com_ports = list_serial_ports()  # Fetch COM ports
        if len(com_ports) == 0:
            log_.error(
                "No com ports found or declared. Use the --device command to set."
            )
            raise UserWarning("No com ports found or declared.")
        device = com_ports[0]

    ramp_model = RampModel(a=60075)
    if rc:
        ramp_model.rc = rc
    if m is not None:
        ramp_model.m = m

    npoints = int(round(maxtime / spacing))

    settings = TraceSettings(
        spacing=spacing,
        ramp_mode=ramp_mode,
        ramp_model=ramp_model,
        i_start=int(round(start_time / spacing)),
        npoints=npoints,
    )

    assert settings.npoints == npoints
    resource = f"ASRL{device_str}::INSTR"
    baudrate = 115200
    with Device(baudrate=baudrate, resource=resource) as device:
        header = setup(
            device=device,
            settings=settings,
            set_timing=((rc is not None) or (m is not None)),
        )
        log_.info(f"header: {header}")
        rxdac = take_trace(device=device, command="RXDAC?",
                           npoints=settings.npoints)
        run_monitor_plot(settings=settings, rxdac=rxdac, device=device)


def main():
    try:
        cli_main()
    except serial.serialutil.SerialException:
        pass


if __name__ == "__main__":
    main()
