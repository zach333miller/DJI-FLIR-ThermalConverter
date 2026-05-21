"""DJI thermal data extraction WITHOUT the DJI Thermal SDK.

DJI thermal R-JPEGs store the raw 16-bit thermal pixel data inside multiple
JPEG APP3 segments (marker 0xFFE3). Concatenating those segments gives a
plain uint16 LE stream sized exactly width * height * 2 bytes.

This is a pragmatic fallback for SDK incompatibility (e.g. SDK older than
camera firmware, or SDK that simply doesn't support a particular model) and
also useful when shipping without the DJI SDK at all.

We do NOT decode to absolute temperature here — the per-pixel value is the
camera's raw sensor count. Conversion to Celsius requires the camera's
calibration (Planck constants + atmospherics), which DJI stores in a
proprietary APP4 'iirp' block. That decoding is not implemented here; for
re-encoding into a FLIR R-JPEG (Tier 2 output), we treat the DJI raw values
as drop-in replacements for FLIR raw values. The resulting FLIR file will be
*format-valid* and open in FLIR-compatible viewers; the displayed
temperatures will reflect the FLIR template's calibration applied to DJI's
raw values, not absolute accuracy.

To get accurate temperatures, prefer the SDK path when available (see
`tsdk.py`).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

import numpy as np
from PIL import Image
import piexif


_SOI = b"\xff\xd8"


@dataclass
class DjiRawData:
    raw_uint16: np.ndarray   # (H, W) uint16 — DJI's raw thermal counts
    width: int
    height: int
    meta: dict[str, Any] = field(default_factory=dict)
    visible_jpeg_bytes: Optional[bytes] = None


def _read_dji_metadata(path: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        exif = piexif.load(str(path))
    except Exception:
        return out
    zeroth = exif.get("0th", {}) or {}
    exif_ifd = exif.get("Exif", {}) or {}
    gps = exif.get("GPS", {}) or {}

    def dec(b):
        return b.decode("utf-8", "replace").strip("\x00").strip() if isinstance(b, bytes) else b

    if piexif.ImageIFD.Make in zeroth:
        out["Make"] = dec(zeroth[piexif.ImageIFD.Make])
    if piexif.ImageIFD.Model in zeroth:
        out["Model"] = dec(zeroth[piexif.ImageIFD.Model])
    if piexif.ExifIFD.DateTimeOriginal in exif_ifd:
        out["DateTimeOriginal"] = dec(exif_ifd[piexif.ExifIFD.DateTimeOriginal])

    def gps_to_deg(t):
        if not t:
            return None
        d, m, s = t
        return d[0] / d[1] + (m[0] / m[1]) / 60.0 + (s[0] / s[1]) / 3600.0

    if piexif.GPSIFD.GPSLatitude in gps:
        lat = gps_to_deg(gps[piexif.GPSIFD.GPSLatitude])
        if lat is not None and gps.get(piexif.GPSIFD.GPSLatitudeRef) == b"S":
            lat = -lat
        out["GPSLatitude"] = lat
    if piexif.GPSIFD.GPSLongitude in gps:
        lon = gps_to_deg(gps[piexif.GPSIFD.GPSLongitude])
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


def _extract_visible(path: Path) -> Optional[bytes]:
    try:
        with Image.open(str(path)) as img:
            buf = BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=92)
            return buf.getvalue()
    except Exception:
        return None


def _extract_app3_payloads(jpeg: bytes) -> list[bytes]:
    """Walk JPEG marker segments and collect payloads of every APP3 (0xFFE3)
    segment. APP3 is where DJI stores raw thermal data."""
    out: list[bytes] = []
    if not jpeg.startswith(_SOI):
        raise ValueError("not a JPEG")
    pos = 2
    while pos < len(jpeg):
        if jpeg[pos] != 0xFF:
            break
        m = jpeg[pos + 1]
        if m in (0xD9, 0xDA):
            break
        seg_len = struct.unpack(">H", jpeg[pos + 2 : pos + 4])[0]
        payload = jpeg[pos + 4 : pos + 2 + seg_len]
        if m == 0xE3:
            out.append(payload)
        pos = pos + 2 + seg_len
    return out


# Known thermal sensor sizes for DJI cameras: byte_count -> (width, height).
# We use these to auto-detect dimensions from the APP3 byte count when the
# EXIF Make/Model doesn't match a known profile (e.g. firmware variations).
_KNOWN_RAW_SIZES: dict[int, tuple[int, int]] = {
    640 * 512 * 2:  (640, 512),    # H20T, M30T, M3T, M4T, M4TD
    1280 * 1024 * 2: (1280, 1024), # H30T
    1024 * 768 * 2: (1024, 768),   # potential future cameras
    320 * 256 * 2:  (320, 256),    # smaller-sensor variants
}


def extract_dji_raw(
    path: Path,
    *,
    expected_width: int = 0,
    expected_height: int = 0,
) -> DjiRawData:
    """Read a DJI thermal R-JPEG and return raw uint16 thermal data, EXIF
    metadata, and the visible-light JPEG bytes — all without the DJI SDK.

    Dimensions are determined in this priority order:
      1. The caller's expected_width/height (from EXIF-based camera profile).
      2. Auto-detection from the APP3 byte count against a known-sensor table.
      3. Failing both, raises with a descriptive error including the byte
         count so the user can report the unknown camera.
    """
    if not path.exists():
        raise FileNotFoundError(path)
    jpeg = path.read_bytes()
    chunks = _extract_app3_payloads(jpeg)
    if not chunks:
        raise ValueError(
            f"{path.name}: no APP3 (thermal) segments found. "
            f"This may not be a DJI thermal R-JPEG."
        )
    raw_bytes = b"".join(chunks)
    n_bytes = len(raw_bytes)

    # Decide on dimensions.
    width = expected_width
    height = expected_height
    expected_bytes = width * height * 2 if (width and height) else 0

    if expected_bytes != n_bytes:
        # EXIF-based dimensions don't match APP3 size. Try auto-detect.
        if n_bytes in _KNOWN_RAW_SIZES:
            width, height = _KNOWN_RAW_SIZES[n_bytes]
        elif expected_bytes > 0 and n_bytes > expected_bytes:
            # APP3 has more data than expected — this can happen if an
            # H30T is mis-detected as a 640x512 camera. Use whichever
            # known size matches.
            for candidate_bytes, (w, h) in _KNOWN_RAW_SIZES.items():
                if candidate_bytes == n_bytes:
                    width, height = w, h
                    break
            else:
                raise ValueError(
                    f"{path.name}: APP3 payload is {n_bytes} bytes, which "
                    f"doesn't match any known DJI sensor size. "
                    f"Known sizes: {sorted(_KNOWN_RAW_SIZES.values())}. "
                    f"This camera may be unsupported — please report the "
                    f"camera model so we can add it."
                )
        else:
            raise ValueError(
                f"{path.name}: APP3 payload is {n_bytes} bytes but caller "
                f"expected {expected_bytes} for {expected_width}x{expected_height}. "
                f"Known sensor sizes: {sorted(_KNOWN_RAW_SIZES.values())}."
            )
        expected_bytes = width * height * 2

    raw = np.frombuffer(
        raw_bytes[:expected_bytes], dtype="<u2"
    ).reshape((height, width)).copy()

    return DjiRawData(
        raw_uint16=raw,
        width=width,
        height=height,
        meta=_read_dji_metadata(path),
        visible_jpeg_bytes=_extract_visible(path),
    )
