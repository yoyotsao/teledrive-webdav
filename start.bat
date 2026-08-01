@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

:: --- tune this for your machine ----------------------------------------- ::
:: Where rclone caches lives is not set here: config.ini names one root and the
:: program lays out meta/, rclone/, local/ and staging/ inside it. Duplicating
:: the path in two files is how they drift apart.
set MOUNT=H:
:: ------------------------------------------------------------------------- ::

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

echo [1/3] starting bridge...
start "TeleDrive bridge" /min "%PY%" bridge.py

echo [2/3] waiting for the bridge to answer...
set /a tries=0
:wait
set /a tries+=1
"%PY%" -c "import sys,urllib.request;urllib.request.urlopen('http://127.0.0.1:8081/rpc/health',timeout=2)" >nul 2>&1
if not errorlevel 1 goto ready
if %tries% GEQ 30 (
  echo [error] bridge did not come up. Run "%PY%" bridge.py in a console to see why.
  goto :fail
)
timeout /t 2 /nobreak >nul
goto wait
:ready
echo       bridge is up.

where rclone >nul 2>&1
if errorlevel 1 (
  echo [error] rclone is not on PATH. Install rclone + WinFsp first:
  echo         winget install Rclone.Rclone
  echo         winget install WinFsp.WinFsp
  goto :fail
)

:: Ask config.py where the rclone cache goes, so there is one answer.
:: Via a file rather than for/f: a quoted command containing its own quotes is
:: exactly what for/f mangles.
"%PY%" -c "from config import load_config; print(load_config().rclone_dir)" > "%TEMP%\cachedir.txt"
set /p RCLONE_CACHE=<"%TEMP%\cachedir.txt"
del "%TEMP%\cachedir.txt" 2>nul
if not defined RCLONE_CACHE (
  echo [error] could not read the cache location from config.ini
  goto :fail
)
if not exist "%RCLONE_CACHE%" mkdir "%RCLONE_CACHE%"
echo       cache: %RCLONE_CACHE%

echo [3/3] mounting %MOUNT% (Ctrl+C here unmounts)
:: Backend options via environment instead of a named remote, so "rclone config"
:: is not needed. They must NOT be inlined into the connection string
:: (":webdav,url=http://...:"): rclone splits remote from path at the first
:: colon, so the "http:" colon truncates the url to "http" and the rest becomes
:: a path. Quoting would work too, but batch quote escaping is brittle.
set RCLONE_WEBDAV_URL=http://127.0.0.1:8081
set RCLONE_WEBDAV_VENDOR=other
:: --vfs-cache-max-age is effectively disabled on purpose: evicting data by age
:: would re-download files that are still wanted, wasting bandwidth and SSD TBW.
:: Capacity-based eviction (--vfs-cache-max-size) is the only policy that fits
:: "a few hours every few months" usage.
rclone mount :webdav: %MOUNT% ^
  --network-mode ^
  --cache-dir "%RCLONE_CACHE%" ^
  --vfs-cache-mode full ^
  --vfs-cache-max-size 160G ^
  --vfs-cache-max-age 8760h ^
  --vfs-cache-min-free-space 20G ^
  --dir-cache-time 1h ^
  --vfs-read-chunk-size 32M ^
  --vfs-read-chunk-size-limit 512M ^
  --transfers 4 ^
  --no-checksum ^
  --rc
goto :eof

:fail
echo.
pause
exit /b 1
