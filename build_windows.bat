@echo off
setlocal
cd /d "%~dp0"
REM Builds dist\ShuleHub.exe. Run this ON Windows (PyInstaller cannot cross-compile).
REM Everything is written to build_log.txt so a failure can be read afterwards.

set "LOG=%~dp0build_log.txt"
echo ShuleHub build started %date% %time% > "%LOG%"
echo.
echo === ShuleHub build ===

REM ---- 1. find a suitable Python (3.12 preferred; 3.11 and 3.13 also work) ----
echo [1/4] Looking for Python...
set "PY="
for %%V in (3.12 3.11 3.13) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
  )
)
if not defined PY (
  python --version >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo.
  echo ERROR: Python was not found.
  echo Install Python 3.12 from https://www.python.org/downloads/ and tick "Add Python to PATH",
  echo then run this file again.
  pause
  exit /b 1
)
%PY% -c "import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] <= (3,13) else 1)" >nul 2>&1
if errorlevel 1 (
  echo.
  echo ERROR: Your Python is too new or too old for this build.
  %PY% --version
  echo Install Python 3.12 from https://www.python.org/downloads/ ^(you can keep your other version^),
  echo then run this file again.
  pause
  exit /b 1
)
%PY% --version
%PY% --version >> "%LOG%" 2>&1

REM ---- 2. clean virtual environment ----
echo [2/4] Preparing a clean environment...
if exist venv rmdir /s /q venv
%PY% -m venv venv >> "%LOG%" 2>&1
if errorlevel 1 goto :fail
call venv\Scripts\activate.bat

REM ---- 3. install what the app needs ----
echo [3/4] Installing packages (needs internet, a few minutes)...
python -m pip install --upgrade pip >> "%LOG%" 2>&1
pip install -r requirements-offline.txt >> "%LOG%" 2>&1
if errorlevel 1 goto :fail

REM ---- 4. build ----
echo [4/4] Building ShuleHub.exe (a few minutes)...
pyinstaller --clean --noconfirm ShuleHub.spec >> "%LOG%" 2>&1
if errorlevel 1 goto :fail

if not exist "dist\ShuleHub.exe" goto :missing

echo.
echo SUCCESS. Your app is at:  %~dp0dist\ShuleHub.exe
echo Close this window and double-click it to run.
pause
exit /b 0

:fail
echo.
echo BUILD FAILED. The last lines of build_log.txt are:
echo ------------------------------------------------------------
powershell -NoProfile -Command "Get-Content -LiteralPath '%LOG%' -Tail 25"
echo ------------------------------------------------------------
echo The full log is in:  %LOG%
echo Send me that file (or a photo of the lines above) and I will fix it.
pause
exit /b 1

:missing
echo.
echo The build finished but dist\ShuleHub.exe is NOT there.
echo Windows Security most likely quarantined it (this happens with new .exe files).
echo   1. Open Windows Security ^> Virus ^& threat protection ^> Protection history.
echo   2. Find ShuleHub.exe, choose Actions ^> Restore ^(or Allow on device^).
echo   3. Add this folder as an exclusion: Virus ^& threat protection settings ^>
echo      Manage settings ^> Exclusions ^> Add or remove exclusions ^> Add a folder.
echo   4. Run this file again.
pause
exit /b 1
