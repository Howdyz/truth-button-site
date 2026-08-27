@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel%==0 (
    python mac_changer_gui.py
) else (
    where py >nul 2>nul
    if %errorlevel%==0 (
        py mac_changer_gui.py
    ) else (
        echo Python was not found on PATH. Install it from https://python.org and try again.
        pause
        exit /b 1
    )
)
pause
