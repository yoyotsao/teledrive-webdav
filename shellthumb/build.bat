@echo off
:: Build the shell thumbnail handler. Needs the MSVC x64 toolchain.
::
:: Flat control flow on purpose: "%ProgramFiles(x86)%" contains parentheses,
:: which terminate an enclosing if(...) block early, and variables set inside a
:: block are not visible to tests in the same block without delayed expansion.
setlocal
cd /d "%~dp0"

:: cl.exe being on PATH does not mean the toolchain environment is set up —
:: INCLUDE/LIB are what the compiler actually needs, so test for those.
if defined INCLUDE goto :compile

set "VSDIR=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer"
if not exist "%VSDIR%\vswhere.exe" goto :novs

:: pushd first so the command inside for/f carries no quoted path — quoting a
:: path with spaces there needs escaping that cmd gets wrong more often than not.
pushd "%VSDIR%"
:: ".\" is required: some shells export NoDefaultCurrentDirectoryInExePath,
:: after which cmd will not find an executable by bare name in the cwd.
for /f "usebackq tokens=*" %%i in (`.\vswhere.exe -latest -products * -property installationPath`) do set "VSPATH=%%i"
popd
if not defined VSPATH goto :novs

call "%VSPATH%\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 goto :novs

:compile
:: /utf-8: the source is UTF-8 and the console codepage here is not.
:: x64 only: Explorer and its thumbnail surrogate are 64-bit on 64-bit Windows.
cl /nologo /LD /O2 /EHsc /W3 /utf-8 /DUNICODE /D_UNICODE ^
   TeleDriveThumb.cpp TeleDriveProps.cpp ^
   /Fe:TeleDriveThumb.dll ^
   /link /DEF:TeleDriveThumb.def
if errorlevel 1 exit /b 1

:: warmshell.exe is part of the product, not a measurement tool: the warm-up
:: needs it to get anything into Windows' own thumbnail cache, and a machine
:: with the DLL but no warmshell.exe is stuck at 3 thumbnails a second instead
:: of 274. Built here so there is one command to remember, and so the two
:: cannot be at different versions.
cl /nologo /O2 /EHsc /W3 /utf-8 /DUNICODE /D_UNICODE warmshell.cpp /Fe:warmshell.exe
if errorlevel 1 exit /b 1

del /q *.obj *.exp *.lib 2>nul
echo [ok] TeleDriveThumb.dll + warmshell.exe
exit /b 0

:novs
echo [error] Visual Studio with the C++ tools was not found.
echo         Install "Desktop development with C++", or run this from a
echo         "x64 Native Tools Command Prompt".
exit /b 1
