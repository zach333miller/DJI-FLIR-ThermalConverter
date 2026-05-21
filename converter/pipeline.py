"""Folder-to-folder conversion pipeline.

Walks an input directory, finds DJI thermal R-JPEGs (any file extension,
identified by APP3 segment presence — the file may have been renamed by the
operator), and writes a FLIR-format radiometric R-JPEG for each into the
output directory.

Visible photos, videos, and other files are skipped. Each photo is processed
via the DJI Thermal SDK v1.8 for factory-grade per-camera calibration.
"""

from __future__ import annotations

import shutil
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from .camera_profiles import detect_profile
from .tier2_flir_jpeg import write_tier2_from_dji_raw
from .tsdk import extract_thermal_data, TSDKNotAvailable, TSDKError


_PHOTO_EXTS = {".jpg", ".jpeg"}


@dataclass
class FileResult:
    src: Path
    dst: Optional[Path] = None
    status: str = "ok"          # "ok", "skipped", "error"
    detail: str = ""


@dataclass
class ConvertSummary:
    input_dir: Path
    output_dir: Path
    scanned: int = 0
    thermal_found: int = 0
    converted: int = 0
    errors: int = 0
    results: list[FileResult] = field(default_factory=list)

    def append(self, r: FileResult) -> None:
        self.results.append(r)
        if r.status == "ok":
            self.converted += 1
        elif r.status == "error":
            self.errors += 1


def _jpeg_has_app3(path: Path) -> bool:
    """Return True if `path` is a JPEG containing an APP3 (0xFFE3) segment.

    DJI thermal R-JPEGs store the raw thermal stream in APP3. Visible JPEGs
    do not. Walks the segment chain by seeking — fast even on multi-MB files.
    """
    try:
        with path.open("rb") as f:
            if f.read(2) != b"\xff\xd8":
                return False
            while True:
                hdr = f.read(2)
                if len(hdr) < 2 or hdr[0] != 0xFF:
                    return False
                marker = hdr[1]
                if marker in (0xDA, 0xD9):
                    return False
                length_bytes = f.read(2)
                if len(length_bytes) < 2:
                    return False
                seg_len = (length_bytes[0] << 8) | length_bytes[1]
                if seg_len < 2:
                    return False
                if marker == 0xE3:
                    return True
                f.seek(seg_len - 2, 1)
    except Exception:
        return False


def _gather_thermal_files(input_dir: Path) -> list[Path]:
    """Return all DJI thermal R-JPEGs in `input_dir` (recursive).

    Uses APP3-segment detection so renamed files are caught. The `_T.JPG`
    fast-path is checked first for speed.
    """
    out: list[Path] = []
    for p in input_dir.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in _PHOTO_EXTS:
            continue
        name = p.name.upper()
        if name.endswith("_T.JPG") or name.endswith("_T.JPEG"):
            out.append(p)
            continue
        if _jpeg_has_app3(p):
            out.append(p)
    return out


def _temperature_to_uint16(temps, profile) -> "np.ndarray":
    """Encode Celsius temperatures into the DJI-style raw uint16 stream that
    `write_tier2_from_dji_raw` consumes.

    DJI's raw stream stores temperature as `temp_K * 100` clamped into uint16,
    which Thermimage/flirpy decode the same way for FLIR data. Using the same
    representation gives the FLIR template's Planck calibration the same
    fixed point that real FLIR cameras emit.
    """
    import numpy as np
    t_k = (temps + 273.15) * 100.0
    return np.clip(t_k, 0, 65535).astype(np.uint16)


def convert_folder(
    input_dir: Path,
    output_dir: Optional[Path] = None,
    *,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> ConvertSummary:
    """Convert every DJI thermal R-JPEG in `input_dir` to FLIR R-JPEG.

    Parameters
    ----------
    input_dir
        Folder containing DJI photos (mix of thermal + visible + videos is OK;
        non-thermal files are skipped).
    output_dir
        Where to put converted files. Defaults to `<input>_FLIR/` alongside
        `input_dir`.
    on_progress
        Optional `callback(done, total, current_filename)` called after each
        file. UI uses this to drive the progress bar.

    Returns
    -------
    ConvertSummary
        Per-file results + counts.
    """
    input_dir = Path(input_dir).resolve()
    if not input_dir.is_dir():
        raise NotADirectoryError(f"{input_dir} is not a directory")

    if output_dir is None:
        output_dir = input_dir.parent / f"{input_dir.name}_FLIR"
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    summary = ConvertSummary(input_dir=input_dir, output_dir=output_dir)

    # Scan
    all_files = [p for p in input_dir.rglob("*") if p.is_file()]
    summary.scanned = len(all_files)
    thermal_files = _gather_thermal_files(input_dir)
    summary.thermal_found = len(thermal_files)

    total = len(thermal_files)
    for i, src in enumerate(thermal_files, start=1):
        if on_progress:
            on_progress(i - 1, total, src.name)
        rel = src.relative_to(input_dir)
        # Output filename keeps source stem + `_FLIR.jpg`. Subfolder structure
        # is flattened so recipients have one folder of files to drag into
        # FLIR Tools.
        dst = output_dir / f"{src.stem}_FLIR.jpg"
        # If the same stem repeats across subfolders, suffix with parent dirs.
        if dst.exists():
            tag = "_".join(part for part in rel.parent.parts if part)
            dst = output_dir / f"{src.stem}__{tag}_FLIR.jpg" if tag else dst

        result = FileResult(src=src, dst=dst)
        try:
            data = extract_thermal_data(src)
            profile = detect_profile(
                data.meta.get("Make"),
                data.meta.get("Model"),
            )
            raw_u16 = _temperature_to_uint16(data.temperature_c, profile)
            write_tier2_from_dji_raw(
                dst,
                raw_u16,
                visible_jpeg_bytes=data.visible_jpeg_bytes,
                profile=profile,
                datetime_original=data.meta.get("DateTimeOriginal"),
                gps_lat=data.meta.get("GPSLatitude"),
                gps_lon=data.meta.get("GPSLongitude"),
                gps_alt_m=data.meta.get("GPSAltitude"),
                camera_model=data.meta.get("Model"),
            )
            result.status = "ok"
            result.detail = (
                f"{profile.display_name} · "
                f"{data.width}x{data.height} · "
                f"{data.temperature_c.min():.1f}–{data.temperature_c.max():.1f} °C"
            )
        except TSDKNotAvailable as e:
            result.status = "error"
            result.detail = f"DJI Thermal SDK not loaded: {e}"
        except TSDKError as e:
            result.status = "error"
            result.detail = (
                f"DJI SDK could not read this file (camera not supported by "
                f"v1.8 SDK, or file corrupted): {e}"
            )
        except Exception as e:
            result.status = "error"
            result.detail = f"{type(e).__name__}: {e}"
            # Stash the traceback in case the user copies the log
            result.detail += "\n" + traceback.format_exc(limit=2)
        summary.append(result)
        if on_progress:
            on_progress(i, total, src.name)

    return summary
