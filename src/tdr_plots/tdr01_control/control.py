# control.py: Low level device control helper functions
import logging
import threading
import time
from contextlib import nullcontext
import pyvisa
from typing import List, Union, Dict, Any, Literal
import numpy as np


from .common import TimingParams, Trace, TraceSettings, TimingCoeffs

log_ = logging.getLogger("tdr_timing")

# Rough ASCII CSV throughput budget for a TRACE? read: at 115200 baud 8n1
# (~11.5 kB/s) each point is ~5-6 bytes ("12345,"), i.e. ~0.6ms/point;
# doubled here for margin and to cover the TCP UART-forwarding path too.
_TRACE_TIMEOUT_BASE_MS = 5000
_TRACE_TIMEOUT_MS_PER_POINT = 1.2

# The TCP endpoint is a bridge onto a real 115200-baud UART (see README), and
# it has little to no buffering on the write side: firing several SCPI
# commands back-to-back over TCP with no pacing reliably wedges it - the
# device stops responding to anything, including queries, until reconnected.
# Confirmed against real hardware: 0s between writes reproduces the hang
# every time; 20ms is reliably enough; this adds a safety margin.
_INTER_COMMAND_DELAY_S = 0.03


def trace_timeout_ms(npoints: int) -> float:
    """Read timeout (ms) big enough to receive an npoints TRACE? response."""
    return _TRACE_TIMEOUT_BASE_MS + npoints * _TRACE_TIMEOUT_MS_PER_POINT


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
        # The live-view GUI runs trace acquisition on a background thread
        # while the settings panel can issue queries/writes from the Tk main
        # thread at the same time. Both share this one serial/TCP connection,
        # so an unguarded interleaving (thread A writes a command, thread B's
        # write lands before A's read consumes the response) desyncs the
        # request/response stream until reconnect. Callers should hold this
        # lock for an entire logical transaction (e.g. a whole take_trace()
        # or setup()), not per write()/read() call.
        self.lock = threading.RLock()

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

    def set_timeout(self, timeout_ms: float) -> None:
        self.timeout = timeout_ms
        self.dev.timeout = timeout_ms

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
    with device.lock:
        if npoints:
            device.set_timeout(trace_timeout_ms(npoints))
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


def _unquote(value: str) -> str:
    """Strip a matching pair of outer double-quotes. The TDR01 returns
    string-valued SCPI responses (e.g. TIMING:RAMP?) quoted, e.g. '"RAMP1"';
    left as-is this breaks any exact-match comparison against the bare
    value ("RAMP1" != '"RAMP1"') and looks wrong when displayed."""
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def setup(device, settings: TraceSettings) -> dict[Any]:
    with device.lock:
        settings = (
            ("AVG", settings.naverages),
            ("TIMING:RAMP", settings.ramp_mode),
            ("TIMING:ISTART", settings.i_start),
            ("TIMING:RESOLUTION", settings.spacing),
            ("TIMING:POINTS", settings.npoints),
            ("PULSES", settings.pulses),
            ("VTX", settings.vbtx),
            ("SLbias", settings.sl_bias),
            ("SBbias", settings.sb_bias),
        )

        queries = {
            "*IDN?",
            "MEASURE:TEMPERATURE:TEMP?",
            "TIMING:RAMP?",
            "AVG?",
            "TIMING:ISTART?",
            "TIMING:RESOLUTION?",
            "TIMING:POINTS?",
            "PULSES?",
            "TIMING:AMPLITUDE?",
            "TIMING:RC?",
            "TIMING:B?",
            "TIMING:M?",
            "VTX?",
            "SLbias?",
            "SBbias?",
        }

        header = {}

        device.flush()
        for key, value in settings:
            command = f"{key} {value}"
            device.write(command)
            log_.debug(command)
            time.sleep(_INTER_COMMAND_DELAY_S)

        device.flush()
        for key in queries:
            log_.debug(key)
            header[key] = _unquote(device.query(key).strip())

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
            except (pyvisa.errors.VisaIOError, TimeoutError) as e:
                # pyvisa raises VisaIOError (not the builtin TimeoutError) on
                # a read timeout, so this previously never caught anything.
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
    lock = getattr(device, "lock", None)
    with lock if lock is not None else nullcontext():
        for key, value in settings.items():
            command = f"{key} {value}"
            device.write(command)
            time.sleep(_INTER_COMMAND_DELAY_S)


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
