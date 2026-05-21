"""FLIR EXIF/XMP tag specification for radiometric R-JPEGs.

These are the tags FLIR analysis software (FLIR Tools, Thermal Studio,
ResearchIR) reads to recognize a JPEG as radiometric and to interpret the
embedded raw thermal stream.

Source of truth: ExifTool's FLIR.pm (Phil Harvey).
  https://exiftool.org/TagNames/FLIR.html

We write these tags via the bundled exiftool.exe binary. The keys here use
exiftool's canonical group:tag form, which is the format we pass to
`exiftool -OVERWRITE_ORIGINAL -TAG=value <file>`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FlirTagSet:
    """Radiometric tag values to write into a FLIR R-JPEG.

    Field names match exiftool's FLIR group tag names (case-insensitive).
    Pass `to_exiftool_args()` to invoke exiftool with all tags in one call.
    """

    raw_thermal_image_width: int
    raw_thermal_image_height: int

    emissivity: float
    object_distance_m: float
    reflected_apparent_temperature_c: float
    atmospheric_temperature_c: float
    relative_humidity: float

    planck_R1: float
    planck_B: float
    planck_F: float
    planck_O: float
    planck_R2: float

    atmospheric_trans_alpha1: float
    atmospheric_trans_alpha2: float
    atmospheric_trans_beta1: float
    atmospheric_trans_beta2: float
    atmospheric_trans_X: float

    # Optional, but FLIR Tools sometimes uses these:
    raw_value_median: int | None = None
    raw_value_range: int | None = None

    def to_exiftool_args(self) -> list[str]:
        args: list[str] = [
            f"-FLIR:RawThermalImageWidth={self.raw_thermal_image_width}",
            f"-FLIR:RawThermalImageHeight={self.raw_thermal_image_height}",
            f"-FLIR:Emissivity={self.emissivity:.4f}",
            f"-FLIR:ObjectDistance={self.object_distance_m:.2f}",
            f"-FLIR:ReflectedApparentTemperature={self.reflected_apparent_temperature_c:.2f}",
            f"-FLIR:AtmosphericTemperature={self.atmospheric_temperature_c:.2f}",
            f"-FLIR:RelativeHumidity={self.relative_humidity:.4f}",
            f"-FLIR:PlanckR1={self.planck_R1:.4f}",
            f"-FLIR:PlanckB={self.planck_B:.4f}",
            f"-FLIR:PlanckF={self.planck_F:.4f}",
            f"-FLIR:PlanckO={self.planck_O:.0f}",
            f"-FLIR:PlanckR2={self.planck_R2:.9f}",
            f"-FLIR:AtmosphericTransAlpha1={self.atmospheric_trans_alpha1:.6f}",
            f"-FLIR:AtmosphericTransAlpha2={self.atmospheric_trans_alpha2:.6f}",
            f"-FLIR:AtmosphericTransBeta1={self.atmospheric_trans_beta1:.6f}",
            f"-FLIR:AtmosphericTransBeta2={self.atmospheric_trans_beta2:.6f}",
            f"-FLIR:AtmosphericTransX={self.atmospheric_trans_X:.6f}",
        ]
        if self.raw_value_median is not None:
            args.append(f"-FLIR:RawValueMedian={self.raw_value_median}")
        if self.raw_value_range is not None:
            args.append(f"-FLIR:RawValueRange={self.raw_value_range}")
        return args


def passthrough_exif_args(
    *,
    datetime_original: str | None,
    gps_lat: float | None,
    gps_lon: float | None,
    gps_alt: float | None,
    camera_model: str | None,
    camera_make: str = "DJI-via-Converter",
) -> list[str]:
    """Build exiftool args for standard EXIF tags carried over from DJI source.

    `datetime_original` should be in EXIF format: 'YYYY:MM:DD HH:MM:SS'.
    """
    args: list[str] = []
    if datetime_original:
        args.append(f"-EXIF:DateTimeOriginal={datetime_original}")
        args.append(f"-EXIF:CreateDate={datetime_original}")
    if camera_model:
        args.append(f"-EXIF:Model={camera_model}")
    args.append(f"-EXIF:Make={camera_make}")
    if gps_lat is not None and gps_lon is not None:
        lat_ref = "N" if gps_lat >= 0 else "S"
        lon_ref = "E" if gps_lon >= 0 else "W"
        args.extend([
            f"-EXIF:GPSLatitude={abs(gps_lat):.7f}",
            f"-EXIF:GPSLatitudeRef={lat_ref}",
            f"-EXIF:GPSLongitude={abs(gps_lon):.7f}",
            f"-EXIF:GPSLongitudeRef={lon_ref}",
        ])
    if gps_alt is not None:
        args.append(f"-EXIF:GPSAltitude={abs(gps_alt):.2f}")
        args.append(f"-EXIF:GPSAltitudeRef={'0' if gps_alt >= 0 else '1'}")
    return args
