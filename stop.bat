@echo off
setlocal
cd /d "%~dp0"

:: Order matters, and every step refuses to go on if the one before it did not
:: finish -- nothing here is ever force-killed under a live reader:
::   1. warmshell (reads through the mount; an orphan pins it until reboot)
::   2. rclone, asked to leave (flushes pending writes through the live bridge)
::   3. the bridge
:: If a step fails, what is still running is left running on purpose: a
:: half-stopped system can still finish its reads, a killed one cannot.

set PY=.venv\Scripts\python.exe

echo [1/3] stopping shell warmers...
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\stop_bridge.ps1" -TimeoutSeconds 10 -WarmersOnly
if errorlevel 1 (
  echo   [error] a warmer would not stop -- rclone, the bridge and the drive were left alone.
  exit /b 1
)

echo [2/3] unmounting (rclone core/quit, no force)...
"%PY%" mountctl.py stop
if errorlevel 1 (
  echo   [error] rclone is still mounted -- the bridge was left running so open reads can finish.
  exit /b 1
)

echo [3/3] stopping the bridge...
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\stop_bridge.ps1" -TimeoutSeconds 10
if errorlevel 1 (
  echo   [error] the bridge could not be stopped.
  exit /b 1
)
echo done.
