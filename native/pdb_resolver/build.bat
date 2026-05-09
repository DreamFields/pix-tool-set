@echo off
setlocal
cd /d "%~dp0"

set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" (
    echo Error: vswhere.exe not found. Install Visual Studio Build Tools.
    exit /b 1
)

for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -requires Microsoft.VisualStudio.Workload.NativeDesktop -property installationPath`) do set "VS_PATH=%%i"
if "%VS_PATH%"=="" (
    for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VS_PATH=%%i"
)
if "%VS_PATH%"=="" (
    echo Error: Visual Studio with C++ tools not found.
    exit /b 1
)

call "%VS_PATH%\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1

if not exist build mkdir build
cd build

cmake .. -G "NMake Makefiles" -DCMAKE_BUILD_TYPE=Release
if %ERRORLEVEL% neq 0 (
    cd ..
    if not exist build_vs mkdir build_vs
    cd build_vs
    cmake .. -G "Visual Studio 17 2022" -A x64
    if %ERRORLEVEL% neq 0 cmake .. -G "Visual Studio 16 2019" -A x64
    cmake --build . --config Release
    cd ..
    exit /b %ERRORLEVEL%
)

cmake --build . --config Release
cd ..

if exist bin\pdb-resolver.exe (
    echo Build successful: bin\pdb-resolver.exe
) else (
    echo Build failed: bin\pdb-resolver.exe was not produced.
    exit /b 1
)
