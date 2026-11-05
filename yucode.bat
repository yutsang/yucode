@echo off
REM Thin wrapper so `yucode <command>` works from this folder without
REM installing the yucode-agent package (which would generate an .exe entry
REM point via pip's console_scripts mechanism -- see README.md's Windows
REM note). This file is plain text, forwards everything to `python -m
REM coding_agent.interface.cli`, and creates nothing else on your machine.
setlocal
where py >nul 2>nul
if %errorlevel%==0 (
    py -m coding_agent.interface.cli %*
) else (
    python -m coding_agent.interface.cli %*
)
endlocal
