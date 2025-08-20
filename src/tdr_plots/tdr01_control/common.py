"""
timing_model.py: Functions for calculating the timing using the
exponential plus linear timing model.
"""

from pydantic import BaseModel, Field, AliasChoices, model_validator
import logging
from typing import Dict, List
from dataclasses import dataclass
import numpy as np

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


class RampModel(BaseModel):
    """
    Basic model of our ramp: v(t) = a*(1-exp(-t/rc))
    """

    a: float = 0
    rc: float = 0
    bf: float = 0
    m: float = 0

    def calc_time(self, v):
        """
        Ramp voltage to time.
        Theres a scew in the actual time due to dv/dt on a given ramp.
        Instead of solving the lambert function on the micro we can iterate
        get close then step through till we get the closest dac value
        """
        # m = self.m
        # t0 = self.t0
        bf = self.bf
        a = self.a
        rc = self.rc

        return -np.log(1 - (v - bf) / a) * rc


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
        return calc_rc(self.c, ra=self.ra, rb=self.rb) * 1e12  # ps

    def to_settings(self):
        return {
            "va": self.quaddac.to_dac(self.va),
            "vb": self.quaddac.to_dac(self.vb),
            "rampb": self.rb < 1e6,
        }


class TraceSettings(BaseModel):
    """ """

    npoints: int = Field(
        default=2500,
        validation_alias=AliasChoices("npoints", "points", "NPOINTS", "get_n_points"),
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
    vbtx: float = Field(
        default=None, validation_alias=AliasChoices("vbtx", "VBTX", "get_vbtx")
    )
    ramp_mode: int = Field(
        default=1, validation_alias=AliasChoices("ramp", "RAMP", "get_ramp_mode")
    )
    ramp_model: RampModel = None

    va: int = 60075
    va0: int = 0
    vb: int = 60075
    vb0: int = 0
    ra: float = 200
    rb: float = 1000
    c: float = 56e-12
    ramp_adc_max: int = 2**16

    @property
    def ramp_vmax(self) -> float:
        if self.ramp_mode == 1:
            return self.va
        if self.ramp_mode == 2:
            return self.vb
        if self.ramp_mode == 3:
            return self.vb + (self.va - self.vb) * self.rb / (self.ra + self.rb)
        raise ValueError(f"Unknown ramp mode {self.ramp_mode}")

    @model_validator(mode="before")
    def set_timing(cls, values: dict):
        if not isinstance(values, dict):
            return values  # in case someone passes a non-dict input

        for field in ("TIMING", "get_timing_params"):
            if field in values:
                timing = values.pop(field)
                a, rc, b, m = timing.strip().split(" ")
                values["ramp_model"] = RampModel(
                    a=float(a), rc=float(rc), b=float(b), m=float(m)
                )
                break
        return values

    @model_validator(mode="after")
    def set_defaults(self):
        if self.vbtx is None:
            dac = TimingDac()
            self.vbtx = dac.to_dac(1)

        if self.ramp_model is None:
            self.ramp_model = get_nominal_ramp_mode_model(self.ramp_mode)

        return self


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

    @property
    def t_nominal(self):
        return self.settings.ramp_model.calc_time(np.asarray(self.rxdac))

    @property
    def trace_volts(self):
        vmax = self.settings.ramp_vmax()
        gain: float = vmax / (self.settings.naverages * self.settings.ramp_adc_max)
        return np.asarray(self.trace) * gain


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
