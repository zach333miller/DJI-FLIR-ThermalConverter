# tsdk_dlls

This folder is empty in the source repo. The DJI Thermal SDK is not
redistributed here — download it directly from DJI before building the
`.exe`.

## Download

<https://www.dji.com/downloads/softwares/dji-thermal-sdk> (free DJI
developer account). **v1.8 or newer is required** — v1.5 returns
error -7 on Matrice 4T (M4T) R-JPEGs.

## What to copy

Unzip the SDK and copy **everything** from
`utility/bin/windows/release_x64/` into this folder:

- `libdirp.dll` — main thermal API
- `libv_dirp.dll`, `libv_girp.dll`, `libv_hirp.dll`, `libv_iirp.dll`,
  `libv_cirp.dll` — per-camera helpers
- `libexif.dll`, `libiconv-2.dll`, `libintl-8.dll` — utility libs
- `MicroIA_Release_x64.dll`, `MicroJPEG_Release_x64.dll`,
  `MicroTA_Release_x64.dll` — image / JPEG internals
- `libv_list.ini` — camera list config

`build.spec` bundles everything in this folder into the final `.exe`
under `tsdk_dlls/`, so the end-user `.exe` is self-contained.

## Verify

```powershell
python -c "import ctypes; ctypes.CDLL('tsdk_dlls/libdirp.dll'); print('OK')"
```

## License note

DJI's SDK license permits redistribution as part of an end-user
application (the bundled `.exe`), but discourages re-hosting the raw
SDK files in a public source repo. That's why this folder is empty in
this repo — build locally, ship the `.exe`.
