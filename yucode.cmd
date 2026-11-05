@echo off
rem Plain-text launcher so `yucode` works from any directory with NO
rem installed executable (pip install would generate a flagged .exe).
rem Self-locating: %~dp0 is this file's own folder, so it keeps working
rem wherever the ZIP is extracted. Add that folder to your USER Path once
rem (Environment Variables dialog - no admin needed) and type `yucode`
rem anywhere; the current directory becomes the workspace.
setlocal
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
python -m coding_agent.interface.cli %*
