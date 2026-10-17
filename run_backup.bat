@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python not found. Please install Python 3 and add it to PATH.
    pause
    exit /b 1
)

python "%~dp0mirror_backup.py" %*
set RC=%ERRORLEVEL%

echo.
pause
exit /b %RC%
