@echo off
REM Build SQLiteBrowser.exe - a single-file desktop app - with PyInstaller.
REM
REM Prerequisites (one-time):
REM   pip install -r requirements.txt
REM   pip install pywebview pyinstaller
REM
REM Output: dist\SQLiteBrowser.exe
REM PyInstaller writes/updates SQLiteBrowser.spec; it can be committed for
REM reproducible rebuilds (run "python -m PyInstaller SQLiteBrowser.spec").
REM
REM Tip: if the app fails to start, swap --windowed for --console below to see
REM the Python traceback in a console window.

cd /d "%~dp0"

echo Cleaning previous build output...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo Building SQLiteBrowser.exe (this can take a minute)...
python -m PyInstaller --noconfirm --onefile --windowed ^
  --name SQLiteBrowser ^
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
echo Done. Executable is at: dist\SQLiteBrowser.exe
pause
