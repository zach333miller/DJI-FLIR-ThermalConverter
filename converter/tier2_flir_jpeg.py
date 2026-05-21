"""Tier 2 output: FLIR-format radiometric R-JPEG.

Strategy: **template-shell with direct FFF binary patching.**

Key reverse-engineered facts about the FLIR R-JPEG format (verified against
Thermimage's IR_2412.jpg + ExifTool 13.57):

  * The file is a normal JPEG. The thermal payload lives in one or more
    APP1 segments whose payloads start with `FLIR\\x00`.
  * Each FLIR APP1 payload has an 8-byte header:
        bytes 0..4: 'FLIR\\x00'
        byte  5:    0x01 (subtype)
        byte  6:    chunk_index
        byte  7:    total_chunks - 1
    followed by a slice of the FFF binary. Multiple APP1 segments must be
    concatenated to recover the full FFF.
  * The FFF container itself uses a **mixed** byte order:
        - Directory header + index entries: BIG-endian
        - Record bodies (CameraInfo, RawThermal): LITTLE-endian
  * FFF directory header:
        bytes 0..3   : 'FFF\\0'
        bytes 24..27 : index_offset (uint32 BE)
        bytes 28..31 : record_count (uint32 BE)
  * Each directory entry is 32 bytes:
        bytes 0..1   : record_type (uint16 BE)
        bytes 2..3   : record_subtype (uint16 BE)
        bytes 4..7   : version (uint32 BE)
        bytes 8..11  : index_id (uint32 BE)
        bytes 12..15 : data_offset (uint32 BE)
        bytes 16..19 : data_length (uint32 BE)
  * Relevant record types: 0x0001 = RawData, 0x0020 = CameraInfo.
  * RawData record body layout (LE):
        bytes 0..1   : image_type (0x0002 = uint16 raw LE)
        bytes 2..3   : width (uint16 LE)
        bytes 4..5   : height (uint16 LE)
        bytes 6..31  : misc (width-1, height-1, ...)
        bytes 32..   : raw uint16 LE pixel data, row-major
  * CameraInfo record body holds Planck constants, atmospherics, emissivity,
    distance, reflected/atmospheric/IR-window temperatures, relative humidity
    at fixed offsets (see `_TEMPLATE_CAL_OFFSETS`).
  * ExifTool reports `RawThermalImageType: TIFF` because it *wraps* the raw
    uint16 stream in a synthesized TIFF on extraction. The on-disk storage
    is bare uint16, not TIFF.

Path B (used here): we do NOT modify CameraInfo. Instead we use the
template's calibration (which we read once at startup) when running
`temp2raw`. The output JPEG, when read by FLIR analysis software with the
template's CameraInfo intact, reconstructs the original DJI temperatures.

The user can still adjust emissivity / distance / reflected-temperature
post-hoc inside FLIR Tools / Thermal Studio — that's the standard FLIR
radiometric workflow and works the same here.

Standard EXIF tags that ExifTool *can* write (DateTimeOriginal, GPS, Model,
Make) are passed through after binary patching.
"""

from __future__ import annotations

import io
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

import numpy as np

from .camera_profiles import CameraProfile
from .flir_format.planck import temp2raw
from .flir_format.tags import passthrough_exif_args


# ---------------------------------------------------------------------------
# Errors

class Tier2NotConfigured(RuntimeError):
    """Template.jpg or exiftool.exe is missing."""


class Tier2ValidationError(RuntimeError):
    """Output file failed post-write validation."""


# ---------------------------------------------------------------------------
# Constants

_SOI = b"\xff\xd8"
_APP1 = 0xE1
_FLIR_ID = b"FLIR\x00"

# Offsets within the CameraInfo record body (LITTLE-endian).
# Names match ExifTool::FLIR.pm entries in FLIR::CameraInfo.
# CameraInfo carries a SECOND copy of sensor dimensions (in addition to
# the RawData record header). FLIR Thermal Studio uses these to interpret
# the raw thermal stream — patching them is critical when the data's
# dimensions differ from the template's source camera.
_CI_SENSOR_WIDTH    = 2     # uint16 LE
_CI_SENSOR_HEIGHT   = 4     # uint16 LE
_CI_SENSOR_WIDTH_M1 = 12    # uint16 LE (width - 1)
_CI_SENSOR_HEIGHT_M1 = 16   # uint16 LE (height - 1)
_CI_EMISSIVITY = 32
_CI_OBJECT_DISTANCE = 36
_CI_REFLECTED_T_K = 40
_CI_ATMOSPHERIC_T_K = 44
_CI_REL_HUMIDITY = 60
_CI_PLANCK_R1 = 88
_CI_PLANCK_B = 92
_CI_PLANCK_F = 96
_CI_ATA1 = 112
_CI_ATA2 = 116
_CI_ATB1 = 120
_CI_ATB2 = 124
_CI_ATX = 128
_CI_PLANCK_O = 776
_CI_PLANCK_R2 = 780
# Raw value range hints — FLIR analysis tools use these to set the default
# display histogram. Wrong values here cause the auto-scale to clip heavily.
_CI_RAW_VALUE_RANGE_MIN = 784  # uint16 LE
_CI_RAW_VALUE_RANGE_MAX = 786  # uint16 LE
_CI_RAW_VALUE_MEDIAN    = 824  # uint16 LE
_CI_RAW_VALUE_RANGE     = 828  # uint16 LE
# Camera/lens model name strings. FLIR Thermal Studio may apply
# camera-model-specific calibration/dewarping based on these — when our
# data dimensions differ from the template's source camera, that
# mismatch can produce mangled rendering.
_CI_CAMERA_MODEL    = 212   # 32-byte string
_CI_CAMERA_PART_NUM = 244   # 16-byte string
_CI_LENS_MODEL      = 368   # 32-byte string
_CI_LENS_PART_NUM   = 400   # 16-byte string


# ---------------------------------------------------------------------------
# Bundled-asset path resolution

def _bundled_root() -> Path:
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base)
    return Path(__file__).resolve().parent.parent


def _exiftool_cmd() -> list[str]:
    # Bypass the exiftool.exe C launcher and invoke perl directly. The
    # launcher fails inside PyInstaller's _MEIPASS temp dir (Perl's @INC
    # ends up empty), but `perl.exe exiftool.pl` works in both source
    # and bundled environments.
    root = _bundled_root()
    perl = root / "exiftool_files" / "perl.exe"
    script = root / "exiftool_files" / "exiftool.pl"
    if not perl.exists() or not script.exists():
        raise Tier2NotConfigured(
            f"exiftool not bundled: missing {perl} or {script}. "
            f"Download from https://exiftool.org and place the Windows "
            f"distribution at the project root."
        )
    return [str(perl), str(script)]


def _template_path() -> Path:
    p = _bundled_root() / "converter" / "flir_format" / "template.jpg"
    if not p.exists():
        raise Tier2NotConfigured(
            f"FLIR template missing at {p}. See template.README.md."
        )
    return p


# ---------------------------------------------------------------------------
# JPEG segment walking

def _iter_jpeg_segments(data: bytes):
    """Yield (marker, payload, seg_start, seg_end) for each marker segment
    after SOI and before SOS/EOI."""
    if not data.startswith(_SOI):
        raise ValueError("Not a JPEG (missing SOI)")
    pos = 2
    while pos < len(data):
        if data[pos] != 0xFF:
            return
        m = data[pos + 1]
        if m in (0xD9, 0xDA):
            return
        seg_len = struct.unpack(">H", data[pos + 2 : pos + 4])[0]
        seg_end = pos + 2 + seg_len
        yield m, data[pos + 4 : seg_end], pos, seg_end
        pos = seg_end


def _extract_flir_app1_chunks(jpeg: bytes) -> list[tuple[int, int, bytes]]:
    """Return [(seg_start, seg_end, payload)] for each FLIR APP1 segment."""
    out: list[tuple[int, int, bytes]] = []
    for marker, payload, start, end in _iter_jpeg_segments(jpeg):
        if marker == _APP1 and payload.startswith(_FLIR_ID):
            out.append((start, end, payload))
    return out


def _reassemble_fff(chunks: list[bytes]) -> bytes:
    """Strip the 8-byte FLIR-chunk header from each payload and concatenate
    in chunk-index order."""
    parts: list[tuple[int, bytes]] = []
    for c in chunks:
        if len(c) < 8:
            continue
        chunk_idx = c[6]
        body = c[8:]
        parts.append((chunk_idx, body))
    parts.sort(key=lambda p: p[0])
    return b"".join(b for _, b in parts)


def _split_fff_into_app1_chunks(fff: bytes, *, chunk_body: int = 65520) -> list[bytes]:
    """Re-split an FFF blob into uniform APP1 chunk payloads.

    JPEG segment length is u16 and *includes* the 2-byte length field, so
    the marker payload must be <= 65533 bytes. The 8-byte FLIR chunk header
    leaves chunk_body <= 65525. We use 65520 for a small safety margin.
    """
    n = max(1, (len(fff) + chunk_body - 1) // chunk_body)
    out: list[bytes] = []
    for i in range(n):
        body = fff[i * chunk_body : (i + 1) * chunk_body]
        header = b"FLIR\x00" + bytes([0x01, i, n - 1])
        out.append(header + body)
    return out


def _split_fff_with_template_sizes(
    fff: bytes, template_body_sizes: list[int]
) -> list[bytes]:
    """Re-split an FFF blob using the template's chunk-body sizes when
    possible, so the output APP1 layout mirrors a real FLIR file.

    If the new FFF is the same size as the template's, sizes line up exactly.
    If the new FFF is larger or smaller (because pixel dimensions changed),
    we use the template's sizes for all chunks except the last, then absorb
    the difference into the last chunk (or add additional chunks as needed,
    capped at JPEG segment-length limits).
    """
    max_body = 65520  # safe under 65525 hard cap
    if not template_body_sizes:
        return _split_fff_into_app1_chunks(fff)

    # Use template sizes for the leading chunks where possible.
    head_sizes = [s for s in template_body_sizes[:-1] if s <= max_body]
    consumed = sum(head_sizes)
    remaining = len(fff) - consumed
    # Tail chunks: as many full max_body chunks as needed.
    tail_sizes: list[int] = []
    while remaining > max_body:
        tail_sizes.append(max_body)
        remaining -= max_body
    if remaining > 0:
        tail_sizes.append(remaining)
    sizes = head_sizes + tail_sizes
    n = len(sizes)

    out: list[bytes] = []
    cursor = 0
    for i, sz in enumerate(sizes):
        body = fff[cursor : cursor + sz]
        header = b"FLIR\x00" + bytes([0x01, i, n - 1])
        out.append(header + body)
        cursor += sz
    return out


def _replace_app1_flir_chunks(jpeg: bytes, new_payloads: list[bytes]) -> bytes:
    """Replace every FLIR APP1 segment in `jpeg` with `new_payloads` (in
    order), inserted at the position of the first original FLIR segment.
    All other JPEG segments are preserved verbatim."""
    flir_segs = _extract_flir_app1_chunks(jpeg)
    if not flir_segs:
        raise ValueError("Template has no FLIR APP1 segments")
    flir_starts = {s[0] for s in flir_segs}

    out = bytearray(_SOI)
    pos = 2
    inserted = False
    while pos < len(jpeg):
        if jpeg[pos] != 0xFF:
            out.extend(jpeg[pos:])
            break
        m = jpeg[pos + 1]
        if m in (0xD9, 0xDA):
            out.extend(jpeg[pos:])
            break
        seg_len = struct.unpack(">H", jpeg[pos + 2 : pos + 4])[0]
        seg_end = pos + 2 + seg_len
        if pos in flir_starts:
            if not inserted:
                for p in new_payloads:
                    out.append(0xFF)
                    out.append(_APP1)
                    out.extend(struct.pack(">H", len(p) + 2))
                    out.extend(p)
                inserted = True
            # drop this FLIR segment
        else:
            out.extend(jpeg[pos:seg_end])
        pos = seg_end
    if not inserted:
        raise ValueError("Failed to insert FLIR chunks into output JPEG")
    return bytes(out)


# ---------------------------------------------------------------------------
# FFF directory parsing & raw-thermal patching

@dataclass
class _FffDir:
    index_offset: int
    record_count: int
    # list of (rec_type, rec_subtype, data_offset, data_length, entry_offset)
    entries: list[tuple[int, int, int, int, int]]


def _parse_fff_directory(fff: bytes) -> _FffDir:
    if not fff.startswith(b"FFF\x00"):
        raise ValueError("FFF magic missing")
    index_off = struct.unpack(">I", fff[24:28])[0]
    rec_count = struct.unpack(">I", fff[28:32])[0]
    if rec_count > 1000:
        raise ValueError(f"unreasonable rec_count {rec_count}")
    entries: list[tuple[int, int, int, int, int]] = []
    for i in range(rec_count):
        e_off = index_off + i * 32
        e = fff[e_off : e_off + 32]
        if len(e) < 32:
            break
        rt, rs = struct.unpack(">HH", e[0:4])
        do, dl = struct.unpack(">II", e[12:20])
        entries.append((rt, rs, do, dl, e_off))
    return _FffDir(index_offset=index_off, record_count=rec_count, entries=entries)


@dataclass
class TemplateCalibration:
    """Calibration parameters extracted from the FLIR template's CameraInfo.

    We use these values when running temp2raw on DJI temperature data, so the
    template's CameraInfo (which we leave unmodified) round-trips correctly
    when FLIR analysis software reads our output.
    """
    emissivity: float
    object_distance_m: float
    reflected_temp_c: float
    atmospheric_temp_c: float
    relative_humidity: float
    R1: float
    B: float
    F: float
    O: int
    R2: float
    ATA1: float
    ATA2: float
    ATB1: float
    ATB2: float
    ATX: float


@lru_cache(maxsize=1)
def _read_template_calibration() -> TemplateCalibration:
    """Read the FLIR template's CameraInfo values once and cache them.

    The output JPEG embeds these same values (we do not modify CameraInfo),
    so encoding our DJI temperatures with these constants guarantees a
    consistent round-trip when FLIR software reads the file.
    """
    template_bytes = _template_path().read_bytes()
    chunks = _extract_flir_app1_chunks(template_bytes)
    fff = _reassemble_fff([c[2] for c in chunks])
    info = _parse_fff_directory(fff)
    ci_entry = next((e for e in info.entries if e[0] == 0x0020), None)
    if ci_entry is None:
        raise Tier2NotConfigured(
            "FLIR template has no CameraInfo record (type 0x0020)."
        )
    rt, rs, do, dl, _ = ci_entry
    ci = fff[do : do + dl]
    f = lambda off: struct.unpack("<f", ci[off : off + 4])[0]
    i = lambda off: struct.unpack("<i", ci[off : off + 4])[0]
    return TemplateCalibration(
        emissivity=f(_CI_EMISSIVITY),
        object_distance_m=f(_CI_OBJECT_DISTANCE),
        reflected_temp_c=f(_CI_REFLECTED_T_K) - 273.15,
        atmospheric_temp_c=f(_CI_ATMOSPHERIC_T_K) - 273.15,
        relative_humidity=f(_CI_REL_HUMIDITY),
        R1=f(_CI_PLANCK_R1),
        B=f(_CI_PLANCK_B),
        F=f(_CI_PLANCK_F),
        O=i(_CI_PLANCK_O),
        R2=f(_CI_PLANCK_R2),
        ATA1=f(_CI_ATA1),
        ATA2=f(_CI_ATA2),
        ATB1=f(_CI_ATB1),
        ATB2=f(_CI_ATB2),
        ATX=f(_CI_ATX),
    )


def _build_raw_thermal_record(raw: np.ndarray) -> bytes:
    """Construct the body of a RawData record from a uint16 (H, W) array.

    Layout (LE):
        u16 image_type = 0x0002 (raw uint16 LE)
        u16 width
        u16 height
        ...26 bytes of header (we fill width-1, height-1 at common offsets)
        ...rows of uint16 LE pixels
    """
    if raw.dtype != np.uint16:
        raw = raw.astype(np.uint16, copy=False)
    h, w = raw.shape
    header = bytearray(32)
    struct.pack_into("<H", header, 0, 0x0002)
    struct.pack_into("<H", header, 2, w)
    struct.pack_into("<H", header, 4, h)
    # Some FLIR firmware also writes width-1, height-1 at offsets 12, 16.
    # The template has 0x027f at @12 and 0x01df at @16 (= 639, 479 for 640x480).
    struct.pack_into("<H", header, 12, w - 1)
    struct.pack_into("<H", header, 16, h - 1)
    return bytes(header) + raw.tobytes(order="C")


def _patch_fff_camera_info(
    fff: bytes,
    *,
    sensor_width: Optional[int] = None,
    sensor_height: Optional[int] = None,
    emissivity: Optional[float] = None,
    object_distance_m: Optional[float] = None,
    reflected_temp_c: Optional[float] = None,
    atmospheric_temp_c: Optional[float] = None,
    relative_humidity: Optional[float] = None,
    raw_value_min: Optional[int] = None,
    raw_value_max: Optional[int] = None,
    raw_value_median: Optional[int] = None,
) -> bytes:
    """Surgically overwrite measurement parameters inside the FLIR
    CameraInfo record (type 0x0020). All values are LE float32 at known
    offsets within the record body. Temperatures are stored in KELVIN
    inside CameraInfo, so we add 273.15 before writing.

    `raw_value_min/max/median` patch the histogram-hint fields that FLIR
    analysis tools use to set the default display contrast. If left None,
    the template's defaults remain — fine for narrow-range images, but
    causes heavy auto-scale clipping when the actual data has wider range
    (e.g. industrial scenes with 50+°C swings).

    Any None parameter is left at the template's default. Returns new
    FFF bytes; never modifies in place.
    """
    info = _parse_fff_directory(fff)
    ci_entry = next((e for e in info.entries if e[0] == 0x0020), None)
    if ci_entry is None:
        return fff
    rt, rs, ci_off, ci_len, _ = ci_entry
    out = bytearray(fff)

    if sensor_width is not None:
        struct.pack_into("<H", out, ci_off + _CI_SENSOR_WIDTH, int(sensor_width) & 0xFFFF)
        struct.pack_into("<H", out, ci_off + _CI_SENSOR_WIDTH_M1, max(0, int(sensor_width) - 1) & 0xFFFF)
    if sensor_height is not None:
        struct.pack_into("<H", out, ci_off + _CI_SENSOR_HEIGHT, int(sensor_height) & 0xFFFF)
        struct.pack_into("<H", out, ci_off + _CI_SENSOR_HEIGHT_M1, max(0, int(sensor_height) - 1) & 0xFFFF)
    if emissivity is not None:
        struct.pack_into("<f", out, ci_off + _CI_EMISSIVITY, float(emissivity))
    if object_distance_m is not None:
        struct.pack_into("<f", out, ci_off + _CI_OBJECT_DISTANCE, float(object_distance_m))
    if reflected_temp_c is not None:
        struct.pack_into("<f", out, ci_off + _CI_REFLECTED_T_K, float(reflected_temp_c) + 273.15)
    if atmospheric_temp_c is not None:
        struct.pack_into("<f", out, ci_off + _CI_ATMOSPHERIC_T_K, float(atmospheric_temp_c) + 273.15)
    if relative_humidity is not None:
        struct.pack_into("<f", out, ci_off + _CI_REL_HUMIDITY, float(relative_humidity))
    if raw_value_min is not None:
        struct.pack_into("<H", out, ci_off + _CI_RAW_VALUE_RANGE_MIN, int(raw_value_min) & 0xFFFF)
    if raw_value_max is not None:
        struct.pack_into("<H", out, ci_off + _CI_RAW_VALUE_RANGE_MAX, int(raw_value_max) & 0xFFFF)
    if raw_value_median is not None:
        struct.pack_into("<H", out, ci_off + _CI_RAW_VALUE_MEDIAN, int(raw_value_median) & 0xFFFF)
    if raw_value_min is not None and raw_value_max is not None:
        rv_range = max(0, min(0xFFFF, int(raw_value_max) - int(raw_value_min)))
        struct.pack_into("<H", out, ci_off + _CI_RAW_VALUE_RANGE, rv_range)

    # Always neutralize camera/lens model strings so FLIR Thermal Studio
    # doesn't try to apply SC660-specific calibration / FOL38-specific lens
    # dewarping to our DJI data.
    def _set_string(off: int, length: int, value: str) -> None:
        b = value.encode("ascii", errors="ignore")[: length - 1]  # leave room for null
        b += b"\x00" * (length - len(b))
        out[ci_off + off : ci_off + off + length] = b

    _set_string(_CI_CAMERA_MODEL,    32, "DJI Thermal")
    _set_string(_CI_CAMERA_PART_NUM, 16, "")
    _set_string(_CI_LENS_MODEL,      32, "")
    _set_string(_CI_LENS_PART_NUM,   16, "")
    return bytes(out)


def _patch_fff_raw_thermal(fff: bytes, new_raw_uint16: np.ndarray) -> bytes:
    """Surgically replace ONLY the raw thermal pixel bytes in the RawData
    (type 0x0001) record. Preserves the FFF directory, every other record
    (CameraInfo, palette, LUT), and the RawData record header verbatim.

    If the new pixel dimensions match the template's, the FFF byte count is
    unchanged (drop-in replacement). If they differ, the RawData record body
    is resized (header still 32 bytes, pixel section grown/shrunk) and the
    directory entry's data_length is updated. Subsequent records are shifted
    and their data_offset entries patched.

    The RawData record header is also patched in place: width/height fields
    at LE offsets +2/+4 (and +12/+16 for w-1/h-1) get updated to match the
    new pixel array.
    """
    if new_raw_uint16.dtype != np.uint16:
        new_raw_uint16 = new_raw_uint16.astype(np.uint16, copy=False)
    new_h, new_w = new_raw_uint16.shape
    new_pixel_bytes = new_raw_uint16.tobytes(order="C")

    info = _parse_fff_directory(fff)
    populated = [e for e in info.entries if e[3] > 0]
    raw_entry = next((e for e in populated if e[0] == 0x0001), None)
    if raw_entry is None:
        raise ValueError("RawData record (type 0x0001) not present in template")
    rt, rs, raw_off, raw_len, raw_entry_off = raw_entry

    HEADER = 32  # bytes of record header before pixel data
    new_record_len = HEADER + len(new_pixel_bytes)
    delta = new_record_len - raw_len  # may be 0, positive, or negative

    # Build the new RawData record body: copy original 32-byte header, patch
    # width/height fields, then append new pixel bytes.
    new_header = bytearray(fff[raw_off : raw_off + HEADER])
    # Existing layout: u16 type @0, u16 width @2, u16 height @4, ...
    # u16 width-1 @12, u16 height-1 @16 (per Thermimage IR_2412.jpg sample).
    struct.pack_into("<H", new_header, 2, new_w)
    struct.pack_into("<H", new_header, 4, new_h)
    struct.pack_into("<H", new_header, 12, new_w - 1)
    struct.pack_into("<H", new_header, 16, new_h - 1)
    new_record = bytes(new_header) + new_pixel_bytes

    # If size unchanged: simple in-place splice.
    if delta == 0:
        out = bytearray(fff)
        out[raw_off : raw_off + raw_len] = new_record
        return bytes(out)

    # Size changed — must shift later content and update directory entries
    # whose data_offset points after raw_off. Also update the RawData entry's
    # data_length, and update the FFF header's index_offset if the index sits
    # after the RawData record (it does in our template at offset ~end).
    out = bytearray(fff[: raw_off])
    out.extend(new_record)
    # Append everything after the original RawData record.
    out.extend(fff[raw_off + raw_len :])

    # Update RawData entry's data_length.
    struct.pack_into(">I", out, raw_entry_off + 16, new_record_len)

    # Shift any entry whose data_offset > raw_off by `delta`. (The index
    # entries themselves live in the index table; their POSITIONS in the FFF
    # may also shift if the index table sits after the RawData record. We
    # update offset values, then update index_offset in the header if needed.)
    for e in info.entries:
        if e[3] == 0:
            continue
        e_rt, e_rs, e_off, e_len, e_entry_off = e
        if e_off > raw_off:
            # The index entry itself may have moved if the index table is
            # past raw_off. Find the entry's NEW position in `out`:
            new_entry_pos = e_entry_off + (delta if e_entry_off > raw_off else 0)
            struct.pack_into(">I", out, new_entry_pos + 12, e_off + delta)

    # Update FFF header's index_offset if the index table sat after raw_off.
    if info.index_offset > raw_off:
        struct.pack_into(">I", out, 24, info.index_offset + delta)

    return bytes(out)


# ---------------------------------------------------------------------------
# Visible-image swap (preserves all JPEG markers including FLIR APP1 chunks)

_EXIF_ID = b"Exif\x00\x00"


def _inject_flir_chunks_into_jpeg(
    visible_jpeg: bytes, flir_chunk_payloads: list[bytes]
) -> bytes:
    """Build a clean FLIR R-JPEG by taking the visible JPEG's image data and
    injecting FLIR APP1 chunks, while STRIPPING all DJI-specific APP markers.

    Real FLIR R-JPEGs only have:
        SOI + APP0(JFIF) + APP1(EXIF) + APP1...APP1(FLIR chunks) +
        DQT + DHT + SOF + SOS + entropy + EOI

    DJI thermal R-JPEGs additionally contain:
        APP1 (XMP drone-dji namespace)
        APP2 (MPF — multi-picture format)
        APP3 (DJI's own raw thermal data — duplicates ours, confuses readers)
        APP4 (iirp — DJI infrared parameters)
        APP5, APP7, APP8 (DJI debug/padding)

    Keeping any of those alongside FLIR chunks confuses FLIR Thermal Studio
    enough that it routes the file to the plain-JPEG handler. So we keep
    only APP0 (JFIF) + APP1 (EXIF, not XMP) and drop everything else.
    """
    if not visible_jpeg.startswith(_SOI):
        raise ValueError("visible_jpeg is not a JPEG")

    out = bytearray(_SOI)
    pos = 2
    inserted = False
    n = len(visible_jpeg)
    while pos < n:
        if visible_jpeg[pos] != 0xFF:
            out.extend(visible_jpeg[pos:])
            return bytes(out)
        m = visible_jpeg[pos + 1]
        if m == 0xD9:  # EOI
            out.extend(visible_jpeg[pos:])
            return bytes(out)
        if m == 0xDA:  # SOS — entropy follows; flush rest verbatim.
            if not inserted:
                _emit_flir_chunks(out, flir_chunk_payloads)
                inserted = True
            out.extend(visible_jpeg[pos:])
            return bytes(out)

        seg_len = struct.unpack(">H", visible_jpeg[pos + 2 : pos + 4])[0]
        seg_end = pos + 2 + seg_len
        payload = visible_jpeg[pos + 4 : seg_end]

        keep = False
        if m == 0xE0:                                 # APP0 (JFIF)
            keep = True
        elif m == _APP1 and payload.startswith(_EXIF_ID):   # APP1 EXIF only
            keep = True
        # Everything else (APP1 XMP, APP2..APP15, COM) is DJI-specific or
        # FLIR-conflicting; drop it.

        if keep:
            out.extend(visible_jpeg[pos:seg_end])
            pos = seg_end
            continue

        # First non-kept marker is our cue to inject FLIR chunks (right after
        # APP0/APP1-EXIF, before any other marker class).
        if not inserted:
            _emit_flir_chunks(out, flir_chunk_payloads)
            inserted = True
        # If this segment is a structural JPEG marker we need (DQT/DHT/SOF),
        # keep it; otherwise drop. Standard JPEG decoders need DQT/DHT/SOF
        # but not COM/APPn we already filtered.
        if m in (0xDB, 0xC4, 0xC0, 0xC2, 0xC1, 0xDD, 0xFE):  # DQT, DHT, SOF, etc.
            out.extend(visible_jpeg[pos:seg_end])
        # Drop anything else silently.
        pos = seg_end

    if not inserted:
        _emit_flir_chunks(out, flir_chunk_payloads)
    return bytes(out)


def _emit_flir_chunks(out: bytearray, payloads: list[bytes]) -> None:
    for p in payloads:
        out.append(0xFF)
        out.append(_APP1)
        out.extend(struct.pack(">H", len(p) + 2))
        out.extend(p)


# ---------------------------------------------------------------------------
# Public API

def write_tier2_from_dji_raw(
    out_path: Path,
    dji_raw: np.ndarray,
    *,
    visible_jpeg_bytes: Optional[bytes],
    profile: CameraProfile,
    datetime_original: Optional[str] = None,
    gps_lat: Optional[float] = None,
    gps_lon: Optional[float] = None,
    gps_alt_m: Optional[float] = None,
    camera_model: Optional[str] = None,
    emissivity: Optional[float] = None,
    object_distance_m: Optional[float] = None,
    reflected_temp_c: Optional[float] = None,
    atmospheric_temp_c: Optional[float] = None,
    relative_humidity: Optional[float] = None,
) -> Path:
    """Build a FLIR R-JPEG directly from DJI's raw uint16 thermal data.

    Optional measurement parameters (typically pulled from the DJI iirp
    APP4 segment) are written into the FLIR CameraInfo block so FLIR
    Tools / Thermal Studio displays the same starting parameters DJI used
    at capture. Any None param falls back to the template's default.
    """
    if dji_raw.ndim != 2:
        raise ValueError(f"dji_raw must be 2-D, got {dji_raw.shape}")
    raw = dji_raw.astype(np.uint16, copy=False)
    return _write_tier2_with_raw(
        out_path, raw,
        visible_jpeg_bytes=visible_jpeg_bytes,
        profile=profile,
        datetime_original=datetime_original,
        gps_lat=gps_lat, gps_lon=gps_lon, gps_alt_m=gps_alt_m,
        camera_model=camera_model,
        ci_emissivity=emissivity,
        ci_object_distance_m=object_distance_m,
        ci_reflected_temp_c=reflected_temp_c,
        ci_atmospheric_temp_c=atmospheric_temp_c,
        ci_relative_humidity=relative_humidity,
    )


def write_tier2_flir_jpeg(
    out_path: Path,
    temperature_c: np.ndarray,
    *,
    visible_jpeg_bytes: Optional[bytes],
    profile: CameraProfile,
    emissivity: float = 0.95,                  # informational only — see note
    object_distance_m: float = 5.0,            # informational only
    reflected_temp_c: float = 25.0,            # informational only
    atmospheric_temp_c: float = 25.0,          # informational only
    relative_humidity: float = 0.5,            # informational only
    datetime_original: Optional[str] = None,
    gps_lat: Optional[float] = None,
    gps_lon: Optional[float] = None,
    gps_alt_m: Optional[float] = None,
    camera_model: Optional[str] = None,
) -> Path:
    """Produce a FLIR-format radiometric JPEG at `out_path`.

    Note on emissivity / distance / reflected temp / atmospherics: in this
    Path-B implementation, the OUTPUT file's CameraInfo carries the FLIR
    template's values, NOT the values passed here. The user can adjust them
    post-hoc inside FLIR Tools / Thermal Studio just like with a real FLIR
    file. The values are accepted for forward compatibility (a future Path-A
    extension can patch CameraInfo too) and noted in the EXIF UserComment.
    """
    if temperature_c.ndim != 2:
        raise ValueError(f"temperature_c must be 2-D, got {temperature_c.shape}")

    cal = _read_template_calibration()

    # Encode our temperature matrix using the *template's* calibration.
    raw = temp2raw(
        temperature_c,
        emissivity=cal.emissivity,
        object_distance_m=cal.object_distance_m,
        reflected_temp_c=cal.reflected_temp_c,
        atmospheric_temp_c=cal.atmospheric_temp_c,
        relative_humidity=cal.relative_humidity,
        R1=cal.R1, B=cal.B, F=cal.F, O=cal.O, R2=cal.R2,
        ATA1=cal.ATA1, ATA2=cal.ATA2,
        ATB1=cal.ATB1, ATB2=cal.ATB2,
        ATX=cal.ATX,
    )
    return _write_tier2_with_raw(
        out_path, raw,
        visible_jpeg_bytes=visible_jpeg_bytes,
        profile=profile,
        datetime_original=datetime_original,
        gps_lat=gps_lat, gps_lon=gps_lon, gps_alt_m=gps_alt_m,
        camera_model=camera_model,
    )


def _write_tier2_with_raw(
    out_path: Path,
    raw: np.ndarray,
    *,
    visible_jpeg_bytes: Optional[bytes],
    profile: CameraProfile,
    datetime_original: Optional[str] = None,
    gps_lat: Optional[float] = None,
    gps_lon: Optional[float] = None,
    gps_alt_m: Optional[float] = None,
    camera_model: Optional[str] = None,
    ci_emissivity: Optional[float] = None,
    ci_object_distance_m: Optional[float] = None,
    ci_reflected_temp_c: Optional[float] = None,
    ci_atmospheric_temp_c: Optional[float] = None,
    ci_relative_humidity: Optional[float] = None,
) -> Path:
    """Inner pipeline: take a uint16 raw thermal matrix, splice it into the
    FLIR template's FFF block, optionally swap visible image, write EXIF
    passthrough, and validate."""
    template_bytes = _template_path().read_bytes()
    # Splice new pixel data into the FFF blob (surgical patching — preserves
    # all other records and FFF directory structure).
    chunks = _extract_flir_app1_chunks(template_bytes)
    fff = _reassemble_fff([c[2] for c in chunks])
    new_fff = _patch_fff_raw_thermal(fff, raw)
    # Compute raw-value histogram hints from the actual data so FLIR's
    # auto-scale picks a sensible display range. Use 1st/99th percentile
    # rather than absolute min/max to ignore hot/dead pixels.
    rv_min = int(np.percentile(raw, 1))
    rv_max = int(np.percentile(raw, 99))
    rv_median = int(np.median(raw))
    # Patch CameraInfo with capture-time params from DJI iirp + range hints
    # + sensor dimensions matching the actual raw data (CRITICAL — FLIR
    # Thermal Studio uses these to interpret raw data layout).
    new_fff = _patch_fff_camera_info(
        new_fff,
        sensor_width=raw.shape[1],
        sensor_height=raw.shape[0],
        emissivity=ci_emissivity,
        object_distance_m=ci_object_distance_m,
        reflected_temp_c=ci_reflected_temp_c,
        atmospheric_temp_c=ci_atmospheric_temp_c,
        relative_humidity=ci_relative_humidity,
        raw_value_min=rv_min,
        raw_value_max=rv_max,
        raw_value_median=rv_median,
    )

    # Re-split using the SAME chunk-body sizes the template used, so the
    # output's APP1 chunk layout mirrors a real FLIR file (which Thermal
    # Studio validates more strictly than ExifTool does).
    template_chunk_bodies = [len(c[2]) - 8 for c in chunks]
    new_chunks = _split_fff_with_template_sizes(new_fff, template_chunk_bodies)

    if visible_jpeg_bytes:
        # Build output from the DJI visible JPEG (renders correctly) and
        # inject FLIR chunks after its existing APPn segments. Falls back to
        # template-with-flir-only if injection fails for some reason.
        try:
            jpeg_with_flir = _inject_flir_chunks_into_jpeg(
                visible_jpeg_bytes, new_chunks
            )
        except Exception:
            jpeg_with_flir = _replace_app1_flir_chunks(template_bytes, new_chunks)
    else:
        jpeg_with_flir = _replace_app1_flir_chunks(template_bytes, new_chunks)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(jpeg_with_flir)

    # Set the writable EXIF tags (DateTime / GPS / Model / Make).
    exif_cmd = _exiftool_cmd()
    args: list[str] = [*exif_cmd, "-overwrite_original"]
    args.extend(passthrough_exif_args(
        datetime_original=datetime_original,
        gps_lat=gps_lat,
        gps_lon=gps_lon,
        gps_alt=gps_alt_m,
        camera_model=camera_model or profile.display_name,
    ))
    args.append(
        f"-EXIF:UserComment="
        f"Converted by DJI-FLIR-Converter (profile={profile.key}); "
        f"radiometric encoded vs FLIR template calibration"
    )
    args.append(str(out_path))
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise Tier2ValidationError(
            f"exiftool EXIF write failed (rc={proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )

    _validate_tier2_output(out_path, exif_cmd, raw.shape)
    return out_path


def _validate_tier2_output(
    path: Path, exif_cmd: list[str], expected_shape: tuple[int, int]
) -> None:
    """Re-read the output via exiftool and confirm raw thermal dimensions
    match. Raises Tier2ValidationError on any mismatch."""
    if not path.exists() or path.stat().st_size == 0:
        raise Tier2ValidationError(f"output missing/empty: {path}")

    proc = subprocess.run(
        [
            *exif_cmd,
            "-s", "-s", "-s",
            "-RawThermalImageWidth",
            "-RawThermalImageHeight",
            "-RawThermalImageType",
            str(path),
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise Tier2ValidationError(
            f"exiftool re-read failed: {proc.stderr.strip()}"
        )
    lines = [l.strip() for l in proc.stdout.splitlines() if l.strip()]
    if len(lines) < 2:
        raise Tier2ValidationError(
            f"exiftool did not return raw dimensions. got: {proc.stdout!r}"
        )
    try:
        rw = int(lines[0])
        rh = int(lines[1])
    except ValueError:
        raise Tier2ValidationError(f"non-numeric raw dimensions: {lines!r}")
    eh, ew = expected_shape
    if rw != ew or rh != eh:
        raise Tier2ValidationError(
            f"raw dimensions mismatch: expected {ew}x{eh}, got {rw}x{rh}"
        )

    # Confirm the raw thermal stream is extractable + non-trivial.
    proc2 = subprocess.run(
        [*exif_cmd, "-b", "-RawThermalImage", str(path)],
        capture_output=True,
    )
    expected_min = ew * eh * 2  # uint16 LE per pixel
    if proc2.returncode != 0 or len(proc2.stdout) < expected_min:
        raise Tier2ValidationError(
            f"raw thermal stream missing/short: rc={proc2.returncode} "
            f"bytes={len(proc2.stdout)} (expected >= {expected_min})"
        )
