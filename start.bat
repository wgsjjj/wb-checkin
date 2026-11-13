@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python not found. Please install Python 3.9+ and retry.
    pause
    exit /b 1
)

echo Starting WorkBuddy Checkin Assistant ...
echo.
python app.py

echo.
echo Server stopped.
pause
