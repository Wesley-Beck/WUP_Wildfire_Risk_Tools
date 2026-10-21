# PyInstaller spec for the one-file WUP_Explorer executable.
#   pip install -r requirements-explorer.txt pyinstaller && pip install --no-deps -e .
#   pyinstaller packaging/wup_explorer.spec        ->  dist/WUP_Explorer(.exe)
# The executable keeps its workspace in WUP_Explorer_data/ next to itself.
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, collect_dynamic_libs

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
STATIC = os.path.join(ROOT, "aorc_tools", "explorer", "static")

datas = [(STATIC, os.path.join("aorc_tools", "explorer", "static"))]
# a source snapshot inside the exe, so research bundles made by the exe also carry runnable source + docs
for top in ("README.md", "USAGE.md", "CLAUDE.md", "setup.py", "requirements.txt", "requirements-explorer.txt",
            "WUP_Explorer.bat", "WUP_Explorer.command", "wup_explorer.sh", "Run_Fire_Checks.bat", "Run_Full_Record.bat"):
    if os.path.exists(os.path.join(ROOT, top)):
        datas.append((os.path.join(ROOT, top), "app_source"))
for sub in ("aorc_tools", "docs", "scripts", "packaging"):
    for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, sub)):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__",)]
        for f in filenames:
            if not f.endswith((".pyc", ".tmp")):
                rel = os.path.relpath(dirpath, ROOT)
                datas.append((os.path.join(dirpath, f), os.path.join("app_source", rel)))
if os.environ.get("GITHUB_SHA"):
    _v = os.path.join(SPECPATH, "APP_VERSION.txt")
    with open(_v, "w") as fh:
        fh.write(os.environ["GITHUB_SHA"] + "\n")
    datas.append((_v, "app_source"))
datas += collect_data_files("tzdata")                    # zoneinfo on Windows has no system tz database
datas += collect_data_files("netCDF4")
datas += collect_data_files("shapely")
datas += collect_data_files("rasterio")                  # GDAL/PROJ data for reading NDVI rasters
datas += collect_data_files("pyproj") if __import__("importlib").util.find_spec("pyproj") else []
hiddenimports = (["cftime", "netCDF4.utils", "openpyxl", "tzdata", "zoneinfo"]
                 + collect_submodules("aorc_tools")
                 + collect_submodules("numcodecs") + collect_submodules("zarr")
                 + collect_submodules("s3fs") + collect_submodules("fsspec.implementations")
                 + collect_submodules("xarray.backends")
                 + collect_submodules("rasterio") + collect_submodules("pystac_client") + collect_submodules("planetary_computer")
                 + ["h5netcdf", "h5py", "requests"])
binaries = collect_dynamic_libs("shapely") + collect_dynamic_libs("netCDF4") + collect_dynamic_libs("rasterio") + collect_dynamic_libs("h5py")

a = Analysis([os.path.join(SPECPATH, "wup_explorer_entry.py")], pathex=[ROOT], binaries=binaries, datas=datas,
             hiddenimports=hiddenimports,
             excludes=["matplotlib", "tkinter", "IPython", "jupyter", "notebook", "pytest", "geopandas", "osmnx",
                       "xclim", "numba", "llvmlite", "PyQt5", "PySide6", "sphinx"],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="WUP_Explorer", console=True, upx=False,
          icon=None, runtime_tmpdir=None, debug=False, strip=False)
