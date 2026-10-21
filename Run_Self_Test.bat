@echo off
REM Checks this computer before a long run: packages, memory, free disk, folders (OneDrive etc.),
REM access to NOAA AORC on S3 / Planetary Computer / AppEEARS, then a short end-to-end test, and
REM one real AORC brick-chunk if S3 answers. Report: WUP_Explorer_data\exports\selftest_*.txt
cd /d "%~dp0"
if exist "WUP_Explorer.exe" (
  "WUP_Explorer.exe" --self-test %*
) else (
  call WUP_Explorer.bat --self-test %*
)
pause
