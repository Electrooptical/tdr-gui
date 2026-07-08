"""
timing_model.py: Functions for calculating the timing using the
exponential plus linear timing model.
"""

import enum
from pydantic import BaseModel, Field, AliasChoices
import copy
import logging
from typing import Dict, List, Literal, Optional, Union
from dataclasses import dataclass
from pydantic import BaseModel, model_validator, StrictInt, StrictStr, ConfigDict
import numpy as np
import scipy as sp
from pathlib import Path
import json

log_ = logging.getLogger("tdr_control")


@dataclass
class Dac:
    vref: float = 3.6
    nbits: int = 16

    @property
    def max(self):
        return self.npoints - 1

    @property
    def npoints(self):
        return 1 << self.nbits

    def to_volts(self, dac):
        return (dac / self.npoints) * self.vref

    def to_dac_f(self, volts: float):
        return volts / self.vref * self.npoints

    def to_dac(self, volts: float):
        return np.round(volts / self.vref * self.npoints)


@dataclass
class TimingDac(Dac):
    def __init__(self):
        super().__init__(vref=3.6, nbits=16)


@dataclass
class QuadDac(Dac):
    def __init__(self):
        super().__init__(vref=3.6, nbits=12)


@dataclass
class Adc:
    vref: float = 3.6
    nbits: int = 12

    @property
    def max(self):
        return self.npoints - 1

    @property
    def npoints(self):
        return 1 << self.nbits

    def to_volts(self, adc):
        return (adc / self.npoints) * self.vref

    def to_adc_f(self, volts: float):
        return volts / self.vref * self.npoints

    def to_adc(self, volts: float):
        return np.round(volts / self.vref * self.npoints)


class Units(enum.Enum):
    volts = 0
    lsb = 1


class RampModel(BaseModel):
    """
    Basic model of our ramp: v(t) = a*(1-exp(-t/rc))
    """

    a: float = 0
    rc: float = 0
    bf: float = 0
    m: float = 0


@dataclass
class MeasurementParams:
    """
    If turning rampb tri state then set rb to a huge number
    """

    va: float = 3.3
    va0: float = 0
    vb: float = 3.3
    vb0: float = 0
    vref: float = 3.6
    ra: float = 200
    rb: float = 1000
    c: float = 56e-12
    tx_set: float = 1

    @property
    def tx_dac(self):
        return self.dac.to_dac(self.tx_set)

    @property
    def dac(self):
        return Dac(vref=self.vref, nbits=16)

    @property
    def quaddac(self):
        return Dac(vref=self.vref, nbits=12)

    @property
    def rc(self):
        ra = self.ra
        rb = self.rb
        return self.c * (ra * rb / (rb + ra)) * 1e12  # ps

    def to_settings(self):
        return {
            "va": self.quaddac.to_dac(self.va),
            "vb": self.quaddac.to_dac(self.vb),
            "rampb": self.rb < 1e6,
        }


class TimingCoeffs(BaseModel):
    """
    Coefficients for an exponential ramp model with offset and slope
    V(T) = a*(1-exp(T/rc)) + b + m*T
    """

    a: Union[float, int, None] = None
    rc: Union[float, int, None] = None
    bf: Union[float, int, None] = None
    m: Union[float, int, None] = None

    def is_sane(self):
        for v in [self.rc, self.a, self.bf, self.m]:
            try:
                if not np.isfinite(v):
                    return False
            except TypeError:
                return False
        return True


"""
    ramp_a: int = 0
    ramp_rc: int = 0
    ramp_bf: int = 0
    ramp_m: int = 0

    @property
    def timing_params(self) -> TimingCoeffs:
        return TimingCoeffs(
            a=self.ramp_a,
            b=self.ramp_bf,
            m=self.ramp_m,
            rc=self.ramp_rc,
        )
"""


class TimingParams(BaseModel):
    npoints: int = 1200
    dt_ps: float = 6
    a: Union[float, int] = 3.3
    rc: Union[float, int] = 0
    bf: Union[float, int] = 0
    m: Union[float, int] = 0
    vbtx: Union[float, int] = 1
    precision: int = 6

    def to_dac(self):
        dac = TimingDac()
        tp = copy.copy(self)
        tp.rc = round(self.rc, self.precision)
        tp.a = round(dac.to_dac(self.a))
        tp.bf = round(dac.to_dac(self.bf))
        tp.m = round(dac.to_dac(self.m), self.precision)
        tp.vbtx = round(dac.to_dac(self.vbtx))
        return tp

    def is_sane(self):
        for v in [self.rc, self.a, self.bf, self.m, self.vbtx]:
            try:
                if not np.isfinite(v):
                    return False
            except TypeError:
                return False
        return True

    def calc_index_from_setting(self, value):
        if self.m != 0:
            raise NotImplementedError("Error")

        log_arg = 1 - (value - self.b) / self.a
        arg = -np.log(log_arg) * self.rc
        return np.round(arg)

    def calc_time_from_setting(self, value):
        return self.calc_index_from_setting(value) * self.dt_ps

    def calc_dac_setting_from_index(self, i):
        return calc_dac_setting(i, a=self.a, rc=self.rc, b=self.b, m=self.m)

    def calc_dac_setting_from_time(self, t):
        return calc_dac_setting(
            round(t / self.dt_ps), a=self.a, rc=self.rc, b=self.b, m=self.m
        )


class TraceSettings(BaseModel):
    """ """

    npoints: int = Field(
        default=2500,
        validation_alias=AliasChoices(
            "npoints", "points", "NPOINTS", "get_n_points"),
    )
    naverages: int = Field(
        default=2, validation_alias=AliasChoices("naverages", "AVG", "get_n_averages")
    )
    spacing: int = Field(
        default=10, validation_alias=AliasChoices("spacing", "SPACING", "get_spacing")
    )
    i_start: int = Field(
        default=0, validation_alias=AliasChoices("i_start", "ISTART", "get_i_start")
    )
    vbtx: Optional[int] = Field(
        default=None, validation_alias=AliasChoices("vbtx", "VBTX", "get_vbtx")
    )

    ramp_mode: Literal["RAMP1", "RAMP2", "BOTH"] = Field(
        default="RAMP1", validation_alias=AliasChoices("ramp", "RAMP", "get_ramp_mode")
    )

    # model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def set_defaults(self):
        if self.vbtx is None:
            dac = TimingDac()
            self.vbtx = int(dac.to_dac(1))

        # if self.ramp_model is None:
        #    ramp_model = get_nominal_ramp_mode_model(self.ramp_mode)
        #    self.ramp_a = ramp_model.a
        #    self.ramp_rc = ramp_model.rc
        #    self.ramp_bf = ramp_model.bf
        #   self.ramp_m = ramp_model.m

        return self


def make_timing_params(params: MeasurementParams, dt_ps=5, npoints=1200):
    """
    a is max voltage of ramp which is va, the power supply
    want rc to be rc / dt so that we can use the index instead of the time
    """
    return TimingParams(
        npoints=npoints,
        a=(params.va - params.calc_vramp(t=0)),
        m=0,
        dt_ps=dt_ps,
        rc=params.rc / dt_ps,  # Time index units
        b=params.calc_vramp(t=0),
    )


def get_nominal_ramp_mode_model(mode):
    """
    Nominal calibration values for linearizing the VBRX values
    """
    modes = [
        RampModel(a=3.3, rc=16510),
        RampModel(a=3.3, rc=76500),
        RampModel(a=3.3, rc=16500),
    ]
    assert (mode > 0) and (mode <= len(modes))
    """
    200*56
    1000*56
    """
    return modes[mode - 1]


class Trace(BaseModel):
    """
    Struct holding the configuration and data for a set of data runs.
    """

    settings: TraceSettings
    rxdac: List[int]
    trace: List[int]

    @property
    def y(self):
        return self.trace

    # @property
    # def t_nominal(self):
    #    return impl.calc_time_from_voltage(np.asarray(self.rxdac))

    # @property
    # def trace_volts(self):
    #    vmax = self.settings.ramp_vmax
    #    gain: float = vmax / (self.settings.naverages * self.settings.ramp_adc_max)
    #    return np.asarray(self.trace) * gain


class CableMeasurement(BaseModel):
    cable_time_ps: float
    reflection_v: Optional[float] = None
    trace: Optional[Trace] = None
    accepted: bool = False

    @model_validator(mode="after")
    def check_voltage_or_trace(self):
        if self.reflection_v is None and self.trace is None:
            raise ValueError(
                "Each trace must have either a voltage or a trace")
        return self


class CalibrationDataSet(BaseModel):
    header: TraceSettings
    runs: List[CableMeasurement]


def write_calibration_data_set(path: Path, data: CalibrationDataSet):
    with path.open("w") as f:
        json.dump(data.model_dump(mode="json"), f, indent=2)


def read_calibration_data_set(path: Path):
    with path.open("r") as f:
        kwargs = json.load(f)
        return CalibrationDataSet(**kwargs)
