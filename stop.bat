@echo off
setlocal
cd /d "%~dp0"

:: Must match start.bat's MOUNT -- only used for the confirmation message below,
:: not for anything that actually unmounts, so a mismatch here costs nothing
:: worse than a wrong drive letter in the printout.
set MOUNT=H:

echo [1/2] stopping rclone mount...
:: Graceful first: start.bat passes --rc, so rclone has an RC API up
:: (127.0.0.1:5572 by default). core/quit lets it flush pending VFS writes and
:: hand the drive back to WinFsp cleanly. taskkill alone can leave %MOUNT%
:: showing up but not responding until Windows notices the process is gone.
where rclone >nul 2>&1
if not errorlevel 1 rclone rc core/quit >nul 2>&1
timeout /t 2 /nobreak >nul
:: Belt and suspenders: if rc/quit did not land (rclone not started with --rc,
:: or already wedged), take it down directly. By image name is fine here --
:: nothing else on this machine runs rclone.
taskkill /F /IM rclone.exe >nul 2>&1

echo [2/2] stopping the bridge...
:: Matched by command line, not image name: python.exe also runs other tools
:: on this machine (see the project's other scripts), and killing every
:: python.exe would take those down as collateral damage.
::
:: The Name check matters for a reason that is easy to miss: this PowerShell
:: command's own command line contains the literal text "bridge.py" (the
:: pattern below), so Win32_Process would find *itself* a match too. Without
:: -and $_.Name -match '^python' it can end up stopping itself mid-pipeline
:: before it reaches the real target.
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python' -and $_.CommandLine -like '*bridge.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo.
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8081 -State Listen -ErrorAction SilentlyContinue) { Write-Host '  [warn] something is still listening on 8081.' } else { Write-Host '  bridge stopped.' }"
if exist %MOUNT%\ (
  echo   [warn] %MOUNT% is still there -- give WinFsp a few seconds to notice.
) else (
  echo   %MOUNT% unmounted.
)
echo done.
