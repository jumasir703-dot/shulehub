@echo off
REM Run this ON Windows (PyInstaller cannot cross-compile).
REM Produces dist\GTimetable.exe

python -m venv venv
call venv\Scripts\activate.bat
pip install -r requirements-offline.txt
pyinstaller GTimetable.spec

echo.
echo Done. Your offline app is at dist\GTimetable.exe
echo Double-click it to run - it opens your browser automatically.
pause
