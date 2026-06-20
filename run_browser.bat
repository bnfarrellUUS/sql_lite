@echo off
REM SQLite Browser launcher - starts the Flask API and opens the app in your browser.
REM Double-click this file to run.

cd /d "%~dp0"

echo Installing/updating Python dependencies (first run only)...
python -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo.
    echo Failed to install dependencies. Make sure Python 3.9+ is installed and on your PATH.
    pause
    exit /b 1
)

echo.
echo Starting SQLite Browser...
echo App will open at http://localhost:5050
echo Press Ctrl+C in this window to stop the server.
echo.

REM Open the browser after a short delay so the server has time to bind the port
start "" /B cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:5050"

python server.py

pause
