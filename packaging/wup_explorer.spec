# PyInstaller spec for the one-file WUP_Explorer executable.
#   pip install -r requirements-explorer.txt pyinstaller && pip install --no-deps -e .
#   pyinstaller packaging/wup_explorer.spec        ->  dist/WUP_Explorer(.exe)
# The executable keeps its workspace in WUP_Explorer_data/ next to itself.
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, collect_dynamic_libs

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
STATIC = os.path.join(ROOT, "aorc_tools", "explorer", "static")

datas = [(STATIC, os.path.join("aorc_tools", "explorer", "static"))]
datas += collect_data_files("tzdata")                    # zoneinfo on Windows has no system tz database
datas += collect_data_files("netCDF4")
datas += collect_data_files("shapely")
hiddenimports = (["cftime", "netCDF4.utils", "openpyxl", "tzdata", "zoneinfo"]
                 + collect_submodules("aorc_tools")
                 + collect_submodules("numcodecs") + collect_submodules("zarr")
                 + collect_submodules("s3fs") + collect_submodules("fsspec.implementations")
                 + collect_submodules("xarray.backends"))
binaries = collect_dynamic_libs("shapely") + collect_dynamic_libs("netCDF4")

a = Analysis([os.path.join(SPECPATH, "wup_explorer_entry.py")], pathex=[ROOT], binaries=binaries, datas=datas,
             hiddenimports=hiddenimports,
             excludes=["matplotlib", "tkinter", "IPython", "jupyter", "notebook", "pytest", "geopandas", "osmnx",
                       "xclim", "numba", "llvmlite", "PyQt5", "PySide6", "sphinx"],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="WUP_Explorer", console=True, upx=False,
          icon=None, runtime_tmpdir=None, debug=False, strip=False)
