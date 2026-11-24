@echo off
setlocal
cd /d "%~dp0"

:: Order: bridge -> rclone -> leftover warmshell.
:: A warmshell can be blocked inside a read on the drive (kernel PageIn wait)
:: that only ends when rclone leaves -- no kill reaches it before that. Waiting
:: for it first deadlocked every stop, so the bridge goes first (that also stops
:: new warmshells being spawned), then rclone is asked to leave (never
:: force-killed), which fails the blocked reads and lets the warmshell exit.
:: Writes rclone has not handed over yet stay in its VFS cache and are sent
:: on the next mount.

set PY=.venv\Scripts\python.exe

echo [1/3] stopping the bridge...
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\stop_bridge.ps1" -TimeoutSeconds 10 -BridgeOnly
if errorlevel 1 (
  echo   [error] the bridge could not be stopped -- rclone and the drive were left alone.
  exit /b 1
)

echo [2/3] unmounting (rclone core/quit, no force)...
"%PY%" mountctl.py stop
if errorlevel 1 (
  echo   [error] rclone is still mounted. Close whatever has the drive open and run stop.bat again.
  exit /b 1
)

echo [3/3] clearing leftover shell warmers...
:: The drive is gone, so nothing can hold them any more.
taskkill /f /im warmshell.exe >nul 2>&1
for /l %%i in (1,1,20) do (
  tasklist /fi "imagename eq warmshell.exe" 2>nul | find /i "warmshell.exe" >nul || goto :done
  timeout /t 1 /nobreak >nul
)
echo   [warn] a warmshell.exe is still running; it holds nothing now but a reboot clears it.
:done
echo done.
