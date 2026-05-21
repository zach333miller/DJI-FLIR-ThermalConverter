# PyInstaller spec for DJI → FLIR Thermal Converter.
#
# Build (from the project's .venv, NOT system Python):
#     .venv\Scripts\pyinstaller.exe --clean --noconfirm build.spec
# Output:
#     dist\DJI-to-FLIR.exe   (single self-contained file, no installer)
#
# The recipient does NOT need Python, the DJI Thermal SDK, exiftool, or any
# other dependency — everything is bundled inside the .exe.

# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

block_cipher = None
ROOT = Path(SPECPATH)


# All runtime data files (DJI SDK DLLs, FLIR template JPEG, exiftool Perl
# distribution). PyInstaller copies these into the extracted _MEIPASS dir
# at startup; our code resolves paths relative to sys._MEIPASS.
datas = [
    (str(ROOT / "tsdk_dlls"), "tsdk_dlls"),
    (str(ROOT / "exiftool_files"), "exiftool_files"),
    (str(ROOT / "converter" / "flir_format" / "template.jpg"),
     "converter/flir_format"),
]

# exiftool.exe is a thin native launcher that loads the bundled Perl from
# exiftool_files\. We ship it as a "binary" so PyInstaller doesn't gzip it.
binaries = [
    (str(ROOT / "exiftool.exe"), "."),
]


a = Analysis(
    ["main.py"],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Drop unused stdlib heavyweights to keep the .exe smaller.
        "unittest",
        "xmlrpc",
        "pydoc",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="DJI-to-FLIR",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX shrinks the exe but can flag false-positives on Windows AV.
    # Leave it off for shareable builds.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # Hide the console window — this is a GUI tool.
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
