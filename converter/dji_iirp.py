"""DJI iirp (Infrared Image Raw Parameters) APP4 segment parser.

DJI thermal R-JPEGs store the user/camera measurement parameters used at
capture time (emissivity, object distance, reflected temperature, etc.) in
a proprietary APP4 segment whose payload begins with the magic bytes
`iirp\\x00`. We use these to populate the FLIR CameraInfo block in the
output so FLIR Tools / Thermal Studio displays sensible defaults that
match what DJI used.

Important limitation: this segment does NOT contain the camera's Planck
calibration constants (R1, B, F, O, R2). Those live inside the DJI Thermal
SDK only. Without them, absolute temperature accuracy in the output FLIR
file relies on the FLIR template's Planck constants applied to DJI's raw
values, which is an approximation.

Layout (reverse-engineered from H20T / H30T / M30T / M4T samples):

    bytes  0..3   : "iirp"
    bytes  4..27  : variable header (sometimes zero, sometimes a small
                    sensor LUT; differs by camera model)
    bytes 28..31  : Reflected temperature (float32 LE, degrees Celsius)
    bytes 32..35  : Object distance (float32 LE, meters)
    bytes 36..39  : Emissivity (float32 LE, 0..1)
    bytes 40..43  : Relative humidity (float32 LE, 0..1 fractional)
    bytes 44..47  : Atmospheric temperature (float32 LE, degrees Celsius)
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class IirpParams:
    reflected_temp_c: float
    object_distance_m: float
    emissivity: float
    relative_humidity: float
    atmospheric_temp_c: float


def _find_iirp_payload(jpeg: bytes) -> Optional[bytes]:
    """Walk JPEG markers and return the payload of the first APP4 segment."""
    pos = 2
    while pos < len(jpeg):
        if jpeg[pos] != 0xFF:
            return None
        m = jpeg[pos + 1]
        if m in (0xD9, 0xDA):
            return None
        seg_len = struct.unpack(">H", jpeg[pos + 2 : pos + 4])[0]
        payload = jpeg[pos + 4 : pos + 2 + seg_len]
        if m == 0xE4:
            return payload
        pos = pos + 2 + seg_len
    return None


def extract_iirp_params(path: Path) -> Optional[IirpParams]:
    """Return DJI capture-time parameters, or None if absent / malformed.

    Returning None is non-fatal — the caller falls back to the FLIR
    template's defaults.
    """
    try:
        jpeg = path.read_bytes()
    except OSError:
        return None
    payload = _find_iirp_payload(jpeg)
    if not payload:
        return None
    iirp_pos = payload.find(b"iirp")
    if iirp_pos < 0:
        return None
    body = payload[iirp_pos:]
    if len(body) < 48:
        return None
    try:
        refl = struct.unpack("<f", body[28:32])[0]
        dist = struct.unpack("<f", body[32:36])[0]
        emis = struct.unpack("<f", body[36:40])[0]
        rh   = struct.unpack("<f", body[40:44])[0]
        atm  = struct.unpack("<f", body[44:48])[0]
    except struct.error:
        return None

    # Sanity-check: physically plausible ranges. A bad parse typically
    # produces NaN, Inf, or values orders of magnitude off.
    if not (-50 <= refl <= 100): return None
    if not (0 < dist <= 10000):  return None
    if not (0.05 <= emis <= 1.0): return None
    if not (0 <= rh <= 1.0):      return None
    if not (-50 <= atm <= 100):  return None

    return IirpParams(
        reflected_temp_c=refl,
        object_distance_m=dist,
        emissivity=emis,
        relative_humidity=rh,
        atmospheric_temp_c=atm,
    )
