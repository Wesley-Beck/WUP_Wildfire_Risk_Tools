@echo off
REM The whole historical record without the browser, resumable:
REM   1. NDVI inputs for FPI (MODIS 2000 onward, Planetary Computer) if they are missing
REM   2. FWI (hourly + daily), FPI, ERC, BI for every brick from 1979-02-01 to the domain end
REM      (already-saved chunks are skipped, so run it again after any interruption)
REM   3. full verification -> WUP_Explorer_data\exports\verify_*.html
REM Extra options pass through, e.g.  --offline   or   --bundle D:\WUP_FDI_bundle.zip
cd /d "%~dp0"
if exist "WUP_Explorer.exe" (
  "WUP_Explorer.exe" --full-record %*
) else (
  call WUP_Explorer.bat --full-record %*
)
pause
