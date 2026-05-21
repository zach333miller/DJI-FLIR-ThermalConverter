"""Planck-equation based temperature <-> raw conversions for FLIR R-JPEGs.

Ported from the Thermimage R package (G. Tattersall, GPL-3):
  https://github.com/gtatters/Thermimage

FLIR stores thermal pixel data as a 16-bit raw value. The relationship between
raw value and apparent black-body temperature uses five camera-specific Planck
constants (R1, B, F, O, R2). The scene temperature additionally depends on the
emissivity of the surface, the reflected apparent temperature, atmospheric
attenuation, distance to object, and ambient/relative humidity. For a complete
treatment see Thermimage::raw2temp.

This module exposes:

  raw2temp(raw, ...)  -> object/scene temperature in Celsius
  temp2raw(t_c, ...)  -> raw 16-bit value (clipped to uint16 range)

`temp2raw` is the inverse used to *generate* a FLIR-compatible raw stream from
a temperature matrix that we obtained from the DJI Thermal SDK. The default
constants are seeded from FLIR sample IR_2412.jpg and should be overridden per
camera profile when accurate calibration is available.
"""

from __future__ import annotations

import math

import numpy as np


# Defaults from FLIR sample IR_2412.jpg (Thermimage). DJI does not publish
# Planck calibration, so these are an approximation when used on DJI data.
DEFAULT_R1 = 21106.77
DEFAULT_B = 1501.0
DEFAULT_F = 1.0
DEFAULT_O = -7340.0
DEFAULT_R2 = 0.012545258

# Atmospheric transmission coefficients (FLIR factory defaults).
DEFAULT_ATA1 = 0.006569
DEFAULT_ATA2 = 0.012620
DEFAULT_ATB1 = -0.002276
DEFAULT_ATB2 = -0.006670
DEFAULT_ATX = 1.9


def _atm_transmission(
    distance_m: float,
    rh: float,
    t_atm_c: float,
    ata1: float,
    ata2: float,
    atb1: float,
    atb2: float,
    atx: float,
) -> float:
    """FLIR atmospheric transmission model.

    rh is fractional (0..1). distance_m is meters. t_atm_c is Celsius.
    """
    h2o = rh * math.exp(
        1.5587
        + 0.06939 * t_atm_c
        - 0.00027816 * t_atm_c**2
        + 0.00000068455 * t_atm_c**3
    )
    sqrt_d = math.sqrt(distance_m)
    tau = atx * math.exp(-sqrt_d * (ata1 + atb1 * math.sqrt(h2o))) + (
        1.0 - atx
    ) * math.exp(-sqrt_d * (ata2 + atb2 * math.sqrt(h2o)))
    return float(tau)


def _planck_raw_from_temp(
    t_c: np.ndarray | float,
    R1: float,
    B: float,
    F: float,
    O: float,
    R2: float,
) -> np.ndarray:
    """Convert apparent black-body temperature (C) to FLIR raw value."""
    t_k = np.asarray(t_c, dtype=np.float64) + 273.15
    return R1 / (R2 * (np.exp(B / t_k) - F)) - O


def _planck_temp_from_raw(
    raw: np.ndarray | float,
    R1: float,
    B: float,
    F: float,
    O: float,
    R2: float,
) -> np.ndarray:
    """Convert FLIR raw value to apparent black-body temperature (C)."""
    raw_a = np.asarray(raw, dtype=np.float64)
    arg = R1 / (R2 * (raw_a + O)) + F
    return B / np.log(arg) - 273.15


def temp2raw(
    temp_c: np.ndarray,
    *,
    emissivity: float = 0.95,
    object_distance_m: float = 5.0,
    reflected_temp_c: float = 25.0,
    atmospheric_temp_c: float = 25.0,
    relative_humidity: float = 0.5,
    R1: float = DEFAULT_R1,
    B: float = DEFAULT_B,
    F: float = DEFAULT_F,
    O: float = DEFAULT_O,
    R2: float = DEFAULT_R2,
    ATA1: float = DEFAULT_ATA1,
    ATA2: float = DEFAULT_ATA2,
    ATB1: float = DEFAULT_ATB1,
    ATB2: float = DEFAULT_ATB2,
    ATX: float = DEFAULT_ATX,
) -> np.ndarray:
    """Inverse of FLIR's raw2temp pipeline.

    Given a per-pixel temperature matrix in Celsius, return a uint16 raw
    matrix that, when fed back through raw2temp with identical parameters,
    reproduces the input temperatures.

    The forward direction is:
      raw_object   = planck_raw(T_object)
      raw_refl     = planck_raw(T_refl)
      raw_atm      = planck_raw(T_atm)
      tau          = atm_transmission(distance, rh, T_atm)
      raw_total    = emiss*tau*raw_object + (1-emiss)*tau*raw_refl
                       + (1-tau)*raw_atm

    We invert it: given temp_c (== T_object), construct raw_object directly,
    then assemble raw_total. (We do not invert through raw_total because we
    *control* T_object — we just re-encode.)
    """
    tau = _atm_transmission(
        object_distance_m,
        relative_humidity,
        atmospheric_temp_c,
        ATA1, ATA2, ATB1, ATB2, ATX,
    )
    raw_object = _planck_raw_from_temp(temp_c, R1, B, F, O, R2)
    raw_refl = float(_planck_raw_from_temp(np.array(reflected_temp_c), R1, B, F, O, R2))
    raw_atm = float(_planck_raw_from_temp(np.array(atmospheric_temp_c), R1, B, F, O, R2))

    raw_total = (
        emissivity * tau * raw_object
        + (1.0 - emissivity) * tau * raw_refl
        + (1.0 - tau) * raw_atm
    )
    raw_clipped = np.clip(raw_total, 0, 65535)
    return raw_clipped.astype(np.uint16)


def raw2temp(
    raw: np.ndarray,
    *,
    emissivity: float = 0.95,
    object_distance_m: float = 5.0,
    reflected_temp_c: float = 25.0,
    atmospheric_temp_c: float = 25.0,
    relative_humidity: float = 0.5,
    R1: float = DEFAULT_R1,
    B: float = DEFAULT_B,
    F: float = DEFAULT_F,
    O: float = DEFAULT_O,
    R2: float = DEFAULT_R2,
    ATA1: float = DEFAULT_ATA1,
    ATA2: float = DEFAULT_ATA2,
    ATB1: float = DEFAULT_ATB1,
    ATB2: float = DEFAULT_ATB2,
    ATX: float = DEFAULT_ATX,
) -> np.ndarray:
    """Forward FLIR conversion: raw 16-bit -> object temperature (C).

    Provided for round-trip validation against temp2raw.
    """
    tau = _atm_transmission(
        object_distance_m,
        relative_humidity,
        atmospheric_temp_c,
        ATA1, ATA2, ATB1, ATB2, ATX,
    )
    raw_refl = float(_planck_raw_from_temp(np.array(reflected_temp_c), R1, B, F, O, R2))
    raw_atm = float(_planck_raw_from_temp(np.array(atmospheric_temp_c), R1, B, F, O, R2))

    raw_a = np.asarray(raw, dtype=np.float64)
    raw_object = (
        raw_a
        - (1.0 - emissivity) * tau * raw_refl
        - (1.0 - tau) * raw_atm
    ) / (emissivity * tau)
    return _planck_temp_from_raw(raw_object, R1, B, F, O, R2)
