@echo off
REM Offline install for a locked-down, no-admin Windows PC.
REM Installs yucode-agent + its pure-Python dependencies from the wheels/
REM folder shipped alongside this script -- no network access needed.
REM
REM If this script fails (e.g. pip itself is blocked by policy), skip it
REM entirely and use the zero-install fallback documented in
REM README-WINDOWS.md instead.

setlocal

echo Installing yucode-agent from local wheels (no network access used)...
py -m pip install --user --no-index --find-links=wheels yucode_agent prompt_toolkit openpyxl
if errorlevel 1 (
    echo.
    echo Install failed. See README-WINDOWS.md "Zero-install fallback" section
    echo -- you can run yucode directly from this folder without installing.
    exit /b 1
)

echo.
echo Install complete. The 'yucode' command may not be on PATH if your user
echo Scripts directory isn't -- the command below always works regardless:
echo.
echo     py -m coding_agent.interface.cli chat --workspace .
echo.
echo Next: copy docs\settings.workbench.yml to %%USERPROFILE%%\.yucode\settings.yml
echo and fill in the FILL-ME values. See README-WINDOWS.md for the full checklist.

endlocal
