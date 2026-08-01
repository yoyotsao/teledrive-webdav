@echo off
setlocal
cd /d "%~dp0"
if defined INCLUDE goto :compile
set "VSDIR=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer"
pushd "%VSDIR%"
for /f "usebackq tokens=*" %%i in (`.\vswhere.exe -latest -products * -property installationPath`) do set "VSPATH=%%i"
popd
call "%VSPATH%\VC\Auxiliary\Build\vcvars64.bat" >nul
:compile
cl /nologo /O2 /EHsc /W3 /utf-8 /DUNICODE /D_UNICODE propprobe.cpp /Fe:propprobe.exe
if errorlevel 1 exit /b 1
del /q *.obj 2>nul
echo [ok] propprobe.exe
