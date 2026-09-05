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

echo [1/3] stopping the bridge...
:: Matched on the command line rather than the window title, which is only set
:: when start.bat launched it. This powershell process is not python.exe, so the
:: filter cannot match itself.
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*bridge.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
:: warmshell is a child of the warm-up, and its 600s deadline lives in the
:: parent's subprocess.run -- so killing the bridge leaves it running with
:: nobody left to time it out. An orphan keeps asking the shell for thumbnails,
:: which keeps reading files through H:, and competes with the browsing the
:: restart was meant to fix. It holds no state worth draining.
taskkill /f /im warmshell.exe >nul 2>&1
timeout /t 2 /nobreak >nul

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
