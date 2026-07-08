# control.py: Low level device control helper functions
import logging
import time
import pyvisa
from typing import List, Union, Dict, Any, Literal
import numpy as np


from .common import TimingParams, Trace, TraceSettings, TimingCoeffs

log_ = logging.getLogger("tdr_timing")


class Device:
    def __init__(
        self,
        resource: pyvisa.Resource,
        baudrate: int = 115200,
        timeout=5e3,
    ):
        self.dev: pyvisa.Resource = resource
        self.baudrate: int = baudrate
        self.timeout: float = timeout

    def __enter__(self):
        self.setup()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.dev:
            try:
                self.dev.close()
            except Exception as e:
                print(f"Warning: Failed to close device: {e}")
        self.dev = None

    def setup(self):
        resource_name = getattr(self.dev, "resource_name", "") or ""
        if resource_name.startswith("ASRL"):
            self.dev.baud_rate = self.baudrate
        else:
            # TCPIP SOCKET resources don't auto-detect line termination the
            # way ASRL/INSTR resources do — set it explicitly to match the
            # TDR01 firmware's "\n"-terminated line protocol.
            self.dev.read_termination = "\n"
            self.dev.write_termination = "\n"
        self.dev.timeout = self.timeout

    def flush(self):
        log_.debug("flush")
        self.dev.write("")
        time.sleep(1)
        # self.dev.clear()
        for f in [
            pyvisa.constants.BufferOperation.discard_read_buffer,
            pyvisa.constants.BufferOperation.discard_read_buffer_no_io,
            pyvisa.constants.BufferOperation.discard_receive_buffer,
            pyvisa.constants.BufferOperation.discard_receive_buffer2,
        ]:
            self.dev.flush(f)

    def write(self, *args, **kwargs):
        return self.dev.write(*args, **kwargs)

    def read(self, *args, **kwargs):
        return self.dev.read(*args, **kwargs)

    def query(self, *args, **kwargs):
        return self.dev.query(*args, **kwargs)

    def query_ascii_values(self, *args, **kwargs):
        return self.dev.query_ascii_values(*args, **kwargs)

    def reset_input_buffer(self):
        self.flush()


def take_trace(
    device: Device, npoints=None, command="TRACE?", tsleep: int = 0.1
) -> np.array:
    device.flush()
    log_.debug(f"Take trace: {command}")
    device.write(command)
    dstr = device.read()
    log_.debug(f"Read {bytes(dstr, 'utf-8')}")
    log_.debug(f"Sleep {tsleep}s")
    time.sleep(tsleep)
    dstr = device.read()
    log_.debug(f"Read {bytes(dstr, 'utf-8')}")
    d = np.array(dstr.strip().split(","), dtype=int)
    log_.debug(f"{d}")

    if npoints:
        assert len(d) == npoints
    return d


def setup(device, settings: TraceSettings) -> dict[Any]:
    settings = (
        ("AVG", settings.naverages),
        ("TIMING:RAMP", settings.ramp_mode),
        ("TIMING:ISTART", settings.i_start),
        ("TIMING:RESOLUTION", settings.spacing),
        ("TIMING:POINTS", settings.npoints),
        ("VTX", settings.vbtx),
    )

    queries = {
        "*IDN?",
        "MEASURE:TEMPERATURE:TEMP?",
        "TIMING:RAMP?",
        "AVG?",
        "TIMING:ISTART?",
        "TIMING:RESOLUTION?",
        "TIMING:POINTS?",
        "TIMING:AMPLITUDE?",
        "TIMING:RC?",
        "TIMING:B?",
        "TIMING:M?",
        "VTX?",
    }

    header = {}

    device.flush()
    for key, value in settings:
        command = f"{key} {value}\n"
        device.write(command)
        msg = f"{command}"
        log_.debug(msg)

    device.flush()
    for key in queries:
        log_.debug(key)
        header[key] = device.query(key).strip()

    return header


def take_traces(device, settings: TraceSettings, ntraces=1, tsleep=0.1) -> List[Trace]:
    header = setup(device, settings)
    npoints = settings.npoints
    ramp_mode = settings.ramp_mode

    log_.info("settings: %s\nqueries %s", str(settings), str(header))

    rxpoints = device.query_ascii_values("RXDAC?")

    traces = []
    for i in range(ntraces):
        time.sleep(tsleep)
        log_.info("Starting Trace %d/%d. Ramp: %s", i + 1, ntraces, str(ramp_mode))
        while True:
            try:
                trace_data = take_trace(device, npoints=npoints, command="TRACE?")
                break
            except TimeoutError as e:
                log_.error(e)
        trace = Trace(rxdac=rxpoints, trace=trace_data, settings=dict(settings))
        traces.append(trace)
    return traces


'''
def run_calibration(
    device: Device,
    cal: CalibrationMeasurement,
    fname: str,
    settings: TraceSettings,
) -> np.array:
    """
    FIXME: This should also read the 3.6VR and check it's levels.
    """
    data_run = take_calibration_trace(
        device=device, fname=fname, settings=settings)

    data_run.write_calibration_file(fname=fname)
    rcs = []
    pt: CalibrationTrace
    for pt in data_run.build():
        if len(pt.t_nominal):
            rc, _ = calibration.calibration_fit(cal=cal)
            rcs.append(rc)
        else:
            log_.warning("Skipping Trace, t_nominal is zero length")
    return np.array(rcs)
'''


def set_timing(
    device: Union[pyvisa.Resource, Device],
    params: Union[TimingParams, TimingCoeffs],
):
    if not params.is_sane():
        raise ValueError("TimingParams invalid: %s", str(params))
    # assert params.a > 10  # Expects a value in dac units

    settings = {
        "TIMING:AMPLITUDE": params.a,
        "TIMING:RC": round(params.rc, 2),
        "TIMING:B": params.bf,
        "TIMING:M": round(params.m, 2),
    }
    for key, value in settings.items():
        command = f"{key} {value}\n"
        device.write(command)


def set_and_store_calibration(
    device: Union[pyvisa.Resource, Device],
    params: Union[TimingParams, TimingCoeffs],
    ramp_mode: int,
    slot=0,
):
    set_timing(device, params)
    ramp_modes = ["A", "B", "C"]
    slot_str = f"{ramp_modes[ramp_mode]}{slot}"
    device.write(f"*SAV {slot_str}")
