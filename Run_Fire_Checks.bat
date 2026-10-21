@echo off
REM Known-fire checks without the browser: computes each fire's season (skipping anything
REM already saved), checks every fire, and writes wup_fire_checks_*.html/.csv/.json to
REM WUP_Explorer_data\exports. Extra options pass through, e.g. --fires pinery-2009
cd /d "%~dp0"
if exist "WUP_Explorer.exe" (
  "WUP_Explorer.exe" --fire-checks %*
) else (
  call WUP_Explorer.bat --fire-checks %*
)
pause
