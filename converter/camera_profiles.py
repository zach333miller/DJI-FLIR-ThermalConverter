"""Per-camera thermal sensor profiles for DJI cameras.

Profiles are looked up from EXIF Make/Model. If the camera is not recognized,
we fall back to GENERIC_PROFILE and surface a warning in the UI.

Sensor dimensions are the radiometric (thermal) frame size, not the visible
preview size. DJI thermal R-JPEGs embed both.

NOTE: Some values (especially the default Planck/atmospheric coefficients) are
approximations seeded from FLIR sample data. They will be refined once we
validate against DJI Thermal Analysis Tool 3 readouts on real samples.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class CameraProfile:
    key: str
    display_name: str
    exif_model_match: tuple[str, ...]
    thermal_width: int
    thermal_height: int
    has_visible_preview: bool = True
    notes: str = ""

    # Default radiometric environment used if the source image lacks the tag.
    default_emissivity: float = 0.95
    default_object_distance_m: float = 5.0
    default_reflected_temp_c: float = 25.0
    default_atmospheric_temp_c: float = 25.0
    default_relative_humidity: float = 0.5

    # Default Planck coefficients seeded from FLIR sample IR_2412.jpg.
    # DJI does not expose its own Planck calibration, so this is an
    # approximation. Refine per-camera once we have validation data.
    planck_R1: float = 21106.77
    planck_B: float = 1501.0
    planck_F: float = 1.0
    planck_O: float = -7340.0
    planck_R2: float = 0.012545258

    # Atmospheric transmission coefficients (FLIR defaults).
    atm_alpha1: float = 0.006569
    atm_alpha2: float = 0.012620
    atm_beta1: float = -0.002276
    atm_beta2: float = -0.006670
    atm_X: float = 1.9


H30T = CameraProfile(
    key="H30T",
    display_name="Zenmuse H30T",
    exif_model_match=("ZH30T", "H30T"),
    thermal_width=1280,
    thermal_height=1024,
    notes="Matrice 350 RTK payload. 1280x1024 LWIR sensor.",
)

H20T = CameraProfile(
    key="H20T",
    display_name="Zenmuse H20T",
    exif_model_match=("ZH20T", "H20T"),
    thermal_width=640,
    thermal_height=512,
    notes="Matrice 300 RTK payload. 640x512 LWIR sensor.",
)

M4T = CameraProfile(
    key="M4T",
    display_name="Matrice 4T",
    exif_model_match=("M4T", "MATRICE4T"),
    thermal_width=640,
    thermal_height=512,
    notes="Matrice 4T thermal payload. 640x512 LWIR sensor.",
)

M4TD = CameraProfile(
    key="M4TD",
    display_name="Matrice 4T Dock (M4TD)",
    exif_model_match=("M4TD",),
    thermal_width=640,
    thermal_height=512,
    notes="Matrice 4T Dock variant. 640x512 LWIR sensor.",
)

M30T = CameraProfile(
    key="M30T",
    display_name="Matrice 30T",
    exif_model_match=("M30T", "MATRICE30T"),
    thermal_width=640,
    thermal_height=512,
    notes=(
        "Matrice 30T thermal payload. 640x512 LWIR sensor. "
        "Note: EXIF PixelXY reports 1280x1024 (visible image), thermal is 640x512."
    ),
)

GENERIC_PROFILE = CameraProfile(
    key="GENERIC",
    display_name="Unrecognized DJI thermal camera",
    exif_model_match=(),
    thermal_width=640,
    thermal_height=512,
    notes=(
        "Fallback profile. Used when EXIF model is not recognized. "
        "Conversion is attempted but accuracy is not guaranteed."
    ),
)


_ALL_PROFILES: tuple[CameraProfile, ...] = (H30T, H20T, M30T, M4TD, M4T)


def detect_profile(make: Optional[str], model: Optional[str]) -> CameraProfile:
    """Pick a profile from EXIF Make/Model. Returns GENERIC_PROFILE on miss.

    Matching is case-insensitive substring against `exif_model_match`.
    """
    if not model:
        return GENERIC_PROFILE
    needle = model.upper().replace(" ", "").replace("-", "").replace("_", "")
    for profile in _ALL_PROFILES:
        for candidate in profile.exif_model_match:
            cand = candidate.upper().replace(" ", "").replace("-", "").replace("_", "")
            if cand in needle:
                return profile
    return GENERIC_PROFILE


def all_known_profiles() -> tuple[CameraProfile, ...]:
    return _ALL_PROFILES
