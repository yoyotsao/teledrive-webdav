@echo off
setlocal
cd /d "%~dp0"

:: One step: start the bridge. The bridge mounts the drive itself once it is
:: serving (mountctl.py, config.ini [bridge] mount_drive), and does nothing if
:: the drive is already mounted -- so running this twice is harmless.

set PY=.venv\Scripts\python.exe
if not exist "%PY%" (
  echo [setup] creating venv...
  python -m venv .venv || goto :fail
  "%PY%" -m pip install --upgrade pip
  "%PY%" -m pip install -r requirements.txt || goto :fail
)
if not exist config.ini (
  echo [error] config.ini is missing. Copy config.example.ini and fill it in.
  goto :fail
)

call :health
if not errorlevel 1 (
  echo [1/2] bridge is already running.
) else (
  echo [1/2] starting bridge...
  start "TeleDrive bridge" /min "%PY%" bridge.py
)

echo [2/2] waiting for the bridge and the mount...
set /a tries=0
:wait
set /a tries+=1
call :health
if not errorlevel 1 (
  "%PY%" mountctl.py check >nul 2>&1
  if not errorlevel 1 goto ready
)
if %tries% GEQ 40 (
  echo [error] not ready. See ^<cache_dir^>\bridge.log and rclone.log
  goto :fail
)
"%SystemRoot%\System32\timeout.exe" /t 2 /nobreak >nul
goto wait
:ready
echo       bridge is up and the drive is mounted.
goto :eof

:health
"%PY%" -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8081/rpc/health',timeout=2)" >nul 2>&1
exit /b %errorlevel%

:fail
echo.
exit /b 1
