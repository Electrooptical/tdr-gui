# control.py: Low level device control helper functions
import logging
import time
from typing import List
import pyvisa

from .common import (
    Trace,
    TraceSettings,
)

log_ = logging.getLogger("tdr_control")

g_rm = pyvisa.ResourceManager("@py")


class Device:
    def __init__(self, resource: str, baudrate: int = 115200, timeout=5e3):
        self.resource = resource
        self.baudrate = baudrate
        self.rm = pyvisa.ResourceManager()
        self.dev: pyvisa.Resource = None
        self.timeout = timeout

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
        self.dev = self.rm.open_resource(self.resource)
        self.dev.baud_rate = 115200
        self.dev.write("E 0")
        self.dev.timeout = self.timeout
        self.flush()

    def flush(self):
        for f in [
            pyvisa.constants.BufferOperation.discard_read_buffer,
            pyvisa.constants.BufferOperation.discard_read_buffer_no_io,
            pyvisa.constants.BufferOperation.discard_receive_buffer,
            pyvisa.constants.BufferOperation.discard_receive_buffer2,
        ]:
            self.dev.flush(f)

    def write(self, *args, **kwargs):
        return self.dev.write(*args, **kwargs)

    def query(self, *args, **kwargs):
        return self.dev.query(*args, **kwargs)

    def query_ascii_values(self, *args, **kwargs):
        return self.dev.query_ascii_values(*args, **kwargs)

    def reset_input_buffer(self):
        self.flush()


def take_trace(device: Device, npoints=None, command="TRACE") -> List[int]:
    d = device.dev.query_ascii_values(command, converter="d", separator=",")
    if npoints:
        assert len(d) == npoints
    return d


def take_traces(
    device, ramp_mode: int, settings: TraceSettings, ntraces=1, tsleep=0.1
) -> List[Trace]:
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
    settings = (
        ("E", 0),
        ("RES", spacing),
        ("ISTART", i_start),
        ("POINTS", npoints),
        ("TIMING", timing_params),
        ("AVG", naverages),
        ("VTX", vbtx),
        ("RAMP", ramp_mode),
    )

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
    for key, value in settings:
        command = f"{key} {value}\n"
        device.write(command)
        device.flush()
        msg = f"{command}"
        log_.debug(msg)

    for key in queries:
        header[key] = device.query(key).strip()
        device.flush()

    log_.info("settings: %s\nqueries %s", str(settings), str(header))

    device.flush()
    while True:
        try:
            rxpoints = take_trace(device, command="RXDAC?", npoints=npoints)
            if len(rxpoints) != npoints:
                msg = "rxpoints is wrong length, retaking %d/%d" % (
                    len(rxpoints),
                    npoints,
                )
                log_.error(msg)
            break
        except TimeoutError as e:
            log_.error(e)

    traces = []
    for i in range(ntraces):
        device.flush()
        time.sleep(tsleep)
        log_.info("Starting Trace %d/%d. Ramp: %d", i + 1, ntraces, ramp_mode)
        while True:
            try:
                trace_data = take_trace(device, npoints=npoints)
                if len(trace_data) != npoints:
                    log_.error(
                        "Trace is wrong length, retaking %d/%d",
                        len(trace_data),
                        npoints,
                    )
                    continue
                break
            except TimeoutError as e:
                log_.error(e)
        trace = Trace(rxdac=rxpoints, trace=trace_data,
                      settings=dict(settings))
        traces.append(trace)
    return traces
