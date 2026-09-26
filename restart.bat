@echo off
setlocal
cd /d "%~dp0"

:: Restart the bridge after a Python change, leaving H: mounted.
::
:: rclone is deliberately left alone: it talks HTTP to 127.0.0.1 and retries, so
:: the mount and its VFS cache survive a bridge restart and Explorer never sees
:: the drive disappear. Killing rclone would unmount H: and throw away the dir
:: cache for nothing. If a *listing* looks stale afterwards, that is rclone's
:: --dir-cache-time, not the bridge: rclone rc vfs/forget.
::
:: A restart drops whatever was mid-debounce in staging/ and uploads/ (the files
:: stay, the timer does not), so check /rpc/status before running this.

set PY=.venv\Scripts\python.exe
if not exist "%PY%" (
  echo [error] no venv. Run start.bat once first.
  goto :fail
)

echo [1/3] stopping shell warmers, then the bridge...
:: The helper closes the process-launch gate, rejects an already orphaned
:: warmer, and gives live children a bounded shutdown before it touches the
:: bridge. It holds the gate until the bridge is gone, so no new child can race
:: into the gap between the process scan and bridge shutdown.
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\stop_bridge.ps1" -TimeoutSeconds 5
if errorlevel 1 (
  echo [error] restart cancelled; bridge is still running.
  goto :fail
)

echo [2/3] starting the bridge...
start "TeleDrive bridge" /min "%PY%" bridge.py

echo [3/3] waiting for it to answer...
set /a tries=0
:wait
set /a tries+=1
"%PY%" -c "import sys,urllib.request;urllib.request.urlopen('http://127.0.0.1:8081/rpc/health',timeout=2)" >nul 2>&1
if not errorlevel 1 goto ready
if %tries% GEQ 30 (
  echo [error] bridge did not come up. See ^<cache_dir^>\bridge.log
  goto :fail
)
timeout /t 2 /nobreak >nul
goto wait
:ready
echo       bridge is up. H: is still mounted.
goto :eof

:fail
echo.
exit /b 1
