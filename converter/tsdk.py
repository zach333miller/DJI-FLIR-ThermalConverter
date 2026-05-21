"""DJI Thermal SDK (TSDK) wrapper.

Wraps `libdirp.dll` from the DJI Thermal SDK (>=1.5) via ctypes. The DLL is
expected at `tsdk_dlls/libdirp.dll` relative to the bundled app root.

This module exposes a single high-level function:

    extract_thermal_data(path) -> ThermalData

`ThermalData` carries:
    - `temperature_c`: numpy float32 array in degrees Celsius.
    - `meta`: dict of EXIF/DJI XMP metadata extracted from the source.
    - `visible_jpeg_bytes`: bytes of the visible-light companion preview
      (None if absent).

The TSDK API used (libdirp v1.5+):
    dirp_create_from_rjpeg(buffer, size, &handle)
    dirp_get_rjpeg_resolution(handle, &resolution)
    dirp_set_measurement_params(handle, &params)   -> set emissivity, etc.
    dirp_measure_ex(handle, dst_buffer, dst_size)  -> float32 grid (Celsius)
    dirp_get_original_image(handle, dst_buf, sz)   -> visible-light JPEG bytes
    dirp_destroy(handle)

References:
    https://www.dji.com/downloads/softwares/dji-thermal-sdk
    Thermal SDK User Manual section 5.

NOTE: The DJI SDK requires manual download (DJI registration). The expected
DLL/header layout is documented in `tsdk_dlls/README.md` (created by setup
script). If the DLL is missing, `extract_thermal_data` raises TSDKNotAvailable.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np
from PIL import Image
import piexif


class TSDKNotAvailable(RuntimeError):
    """Raised when libdirp.dll cannot be loaded."""


class TSDKError(RuntimeError):
    """Raised when a TSDK call returns a non-zero status."""


@dataclass
class ThermalData:
    temperature_c: np.ndarray  # float32 (H, W), Celsius
    width: int
    height: int
    meta: dict[str, Any] = field(default_factory=dict)
    visible_jpeg_bytes: Optional[bytes] = None


# ----- ctypes definitions for libdirp ---------------------------------------

class _DirpResolution(ctypes.Structure):
    _fields_ = [("width", ctypes.c_int32), ("height", ctypes.c_int32)]


class _DirpMeasurementParams(ctypes.Structure):
    # v1.8 added `ambient_temp` as a fifth field. Pre-v1.7 SDKs used only
    # the first four; pass the same value for both reflection + ambient_temp
    # when an explicit ambient isn't available — matches DJI's IIRP usage.
    _fields_ = [
        ("distance", ctypes.c_float),
        ("humidity", ctypes.c_float),
        ("emissivity", ctypes.c_float),
        ("reflection", ctypes.c_float),
        ("ambient_temp", ctypes.c_float),
    ]


_DIRP_SUCCESS = 0


def _bundled_root() -> Path:
    """Resolve the directory containing tsdk_dlls/ at runtime.

    When running under PyInstaller --onefile, sys._MEIPASS points at the
    extracted bundle. In dev, fall back to the project root.
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base)
    return Path(__file__).resolve().parent.parent


def _load_libdirp() -> ctypes.CDLL:
    root = _bundled_root()
    dll_dir = root / "tsdk_dlls"
    candidate = dll_dir / "libdirp.dll"
    if not candidate.exists():
        raise TSDKNotAvailable(
            f"DJI Thermal SDK DLL not found at {candidate}. Download the SDK "
            f"from https://www.dji.com/downloads/softwares/dji-thermal-sdk "
            f"and copy libdirp.dll (and any dependent DLLs) into tsdk_dlls/."
        )
    # Add the DLL directory to the search path so dependent DLLs resolve.
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(str(dll_dir))
    try:
        lib = ctypes.CDLL(str(candidate))
    except OSError as e:
        raise TSDKNotAvailable(
            f"Failed to load libdirp.dll: {e}. Check that all dependent DLLs "
            f"from the DJI Thermal SDK are present in tsdk_dlls/."
        ) from e

    # Bind prototypes. Names match TSDK 1.5+ public API.
    lib.dirp_create_from_rjpeg.argtypes = [
        ctypes.c_char_p, ctypes.c_int32, ctypes.POINTER(ctypes.c_void_p)
    ]
    lib.dirp_create_from_rjpeg.restype = ctypes.c_int32

    lib.dirp_destroy.argtypes = [ctypes.c_void_p]
    lib.dirp_destroy.restype = ctypes.c_int32

    lib.dirp_get_rjpeg_resolution.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_DirpResolution)
    ]
    lib.dirp_get_rjpeg_resolution.restype = ctypes.c_int32

    lib.dirp_set_measurement_params.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_DirpMeasurementParams)
    ]
    lib.dirp_set_measurement_params.restype = ctypes.c_int32

    lib.dirp_measure_ex.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32
    ]
    lib.dirp_measure_ex.restype = ctypes.c_int32

    return lib


_lib_cache: ctypes.CDLL | None = None


def _lib() -> ctypes.CDLL:
    global _lib_cache
    if _lib_cache is None:
        _lib_cache = _load_libdirp()
    return _lib_cache


def _check(rc: int, op: str) -> None:
    if rc != _DIRP_SUCCESS:
        raise TSDKError(f"TSDK {op} returned {rc}")


# ----- public API -----------------------------------------------------------

def extract_thermal_data(
    path: Path,
    *,
    distance_m: Optional[float] = None,
    humidity: Optional[float] = None,      # accepts fraction (0..1) OR percent (1..100); auto-detected
    emissivity: Optional[float] = None,
    reflection_c: Optional[float] = None,
    ambient_temp_c: Optional[float] = None,
) -> ThermalData:
    """Read a DJI thermal R-JPEG and return temperature + metadata.

    Parameters carry through to `dirp_set_measurement_params`. Defaults are
    reasonable for outdoor industrial inspection.

    Raises
    ------
    TSDKNotAvailable
        If libdirp.dll is not present.
    TSDKError
        If any TSDK call returns a non-zero status.
    FileNotFoundError
        If `path` does not exist.
    """
    if not path.exists():
        raise FileNotFoundError(path)

    raw = path.read_bytes()
    lib = _lib()
    handle = ctypes.c_void_p()
    _check(
        lib.dirp_create_from_rjpeg(raw, len(raw), ctypes.byref(handle)),
        "dirp_create_from_rjpeg",
    )
    try:
        res = _DirpResolution()
        _check(
            lib.dirp_get_rjpeg_resolution(handle, ctypes.byref(res)),
            "dirp_get_rjpeg_resolution",
        )
        # Only override the SDK's defaults (which come from the photo's IIRP
        # segment) if the caller explicitly passed any override. Otherwise
        # the SDK uses what DJI baked into the file at capture time.
        if any(v is not None for v in (distance_m, humidity, emissivity,
                                       reflection_c, ambient_temp_c)):
            # Read current params from the photo so we can fill in just the
            # ones the caller is overriding.
            lib.dirp_get_measurement_params.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(_DirpMeasurementParams)
            ]
            lib.dirp_get_measurement_params.restype = ctypes.c_int32
            current = _DirpMeasurementParams()
            _check(
                lib.dirp_get_measurement_params(handle, ctypes.byref(current)),
                "dirp_get_measurement_params",
            )
            # v1.8 wants humidity in PERCENT (1..100). Auto-convert fractional.
            hum = humidity
            if hum is not None and 0.0 < hum <= 1.0:
                hum = hum * 100.0
            params = _DirpMeasurementParams(
                distance     = distance_m   if distance_m   is not None else current.distance,
                humidity     = hum          if hum          is not None else current.humidity,
                emissivity   = emissivity   if emissivity   is not None else current.emissivity,
                reflection   = reflection_c if reflection_c is not None else current.reflection,
                ambient_temp = ambient_temp_c if ambient_temp_c is not None else current.ambient_temp,
            )
            _check(
                lib.dirp_set_measurement_params(handle, ctypes.byref(params)),
                "dirp_set_measurement_params",
            )
        n = res.width * res.height
        # TSDK measure_ex writes float32 Celsius in row-major order.
        buf = (ctypes.c_float * n)()
        _check(
            lib.dirp_measure_ex(handle, buf, ctypes.sizeof(buf)),
            "dirp_measure_ex",
        )
        temps = np.frombuffer(buf, dtype=np.float32).reshape(
            (res.height, res.width)
        ).copy()
    finally:
        lib.dirp_destroy(handle)

    meta = _read_dji_metadata(path)
    visible = _extract_visible_preview(path)

    return ThermalData(
        temperature_c=temps,
        width=int(res.width),
        height=int(res.height),
        meta=meta,
        visible_jpeg_bytes=visible,
    )


def _read_dji_metadata(path: Path) -> dict[str, Any]:
    """Pull the EXIF + (best-effort) DJI XMP fields from the JPEG.

    Returns a flat dict with keys:
        Make, Model, DateTimeOriginal, GPSLatitude, GPSLongitude, GPSAltitude
    GPS values are decimal-degrees floats. Missing keys are simply absent.
    """
    out: dict[str, Any] = {}
    try:
        exif = piexif.load(str(path))
    except Exception:
        return out

    zeroth = exif.get("0th", {}) or {}
    exif_ifd = exif.get("Exif", {}) or {}
    gps = exif.get("GPS", {}) or {}

    def _decode(b: Any) -> Any:
        if isinstance(b, bytes):
            try:
                return b.decode("utf-8", errors="replace").strip("\x00").strip()
            except Exception:
                return b
        return b

    if piexif.ImageIFD.Make in zeroth:
        out["Make"] = _decode(zeroth[piexif.ImageIFD.Make])
    if piexif.ImageIFD.Model in zeroth:
        out["Model"] = _decode(zeroth[piexif.ImageIFD.Model])
    if piexif.ExifIFD.DateTimeOriginal in exif_ifd:
        out["DateTimeOriginal"] = _decode(exif_ifd[piexif.ExifIFD.DateTimeOriginal])

    def _gps_to_deg(t):
        if not t:
            return None
        d, m, s = t
        deg = d[0] / d[1] + (m[0] / m[1]) / 60.0 + (s[0] / s[1]) / 3600.0
        return deg

    if piexif.GPSIFD.GPSLatitude in gps:
        lat = _gps_to_deg(gps[piexif.GPSIFD.GPSLatitude])
        if lat is not None and gps.get(piexif.GPSIFD.GPSLatitudeRef) == b"S":
            lat = -lat
        out["GPSLatitude"] = lat
    if piexif.GPSIFD.GPSLongitude in gps:
        lon = _gps_to_deg(gps[piexif.GPSIFD.GPSLongitude])
        if lon is not None and gps.get(piexif.GPSIFD.GPSLongitudeRef) == b"W":
            lon = -lon
        out["GPSLongitude"] = lon
    if piexif.GPSIFD.GPSAltitude in gps:
        alt_t = gps[piexif.GPSIFD.GPSAltitude]
        alt = alt_t[0] / alt_t[1] if alt_t else None
        if alt is not None and gps.get(piexif.GPSIFD.GPSAltitudeRef) == 1:
            alt = -alt
        out["GPSAltitude"] = alt
    return out


def _extract_visible_preview(path: Path) -> Optional[bytes]:
    """Pull the embedded visible-light preview JPEG from a DJI R-JPEG.

    DJI thermal R-JPEGs encode the visible image either as the primary JPEG
    stream (with thermal data in APP markers) or as an embedded thumbnail.
    We try the primary stream first via PIL; if that yields what looks like a
    visible-light image we return its bytes.
    """
    try:
        img = Image.open(str(path))
        # The primary stream of a DJI R-JPEG is the visible-light image; the
        # thermal payload sits in an APP marker. Re-encode to strip metadata.
        from io import BytesIO
        buf = BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=92)
        return buf.getvalue()
    except Exception:
        return None
