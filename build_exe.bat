@echo off
REM Build the SQLite Browser desktop app with PyInstaller.
REM
REM Prerequisites (one-time):
REM   pip install -r requirements.txt
REM   pip install pywebview pyinstaller
REM
REM Output: dist\SQLiteBrowser\SQLiteBrowser.exe (run this; keep the folder intact)
REM
REM NOTE: This uses --onedir (a folder), NOT --onefile. pywebview's WebView2
REM window does not appear from a --onefile build (the temp-extraction model
REM breaks the WebView2 host), so a one-folder build is required. To hand
REM someone a single file, zip the dist\SQLiteBrowser folder.
REM
REM Tip: if the app fails to start, swap --windowed for --console below to see
REM the Python traceback in a console window.

cd /d "%~dp0"

echo Cleaning previous build output...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo Building SQLite Browser (this can take a minute)...
python -m PyInstaller --noconfirm --onedir --windowed ^
  --name SQLiteBrowser ^
  --icon icon.ico ^
  --add-data "static;static" ^
  --exclude-module pytest ^
  desktop.py

if errorlevel 1 (
    echo.
    echo Build FAILED. See the PyInstaller output above.
    pause
    exit /b 1
)

echo.
echo Done. Run: dist\SQLiteBrowser\SQLiteBrowser.exe
pause
