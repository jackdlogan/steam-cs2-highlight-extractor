@echo off
REM ============================================================
REM  build_tauri.bat  —  Full production build script
REM  Builds: Python server sidecar + Tauri app bundle
REM ============================================================

echo.
echo [1/3] Building Python server sidecar with PyInstaller...
echo -------------------------------------------------------
pyinstaller server.spec --noconfirm --clean
if errorlevel 1 (
    echo ERROR: PyInstaller build failed.
    pause
    exit /b 1
)

echo.
echo [2/3] Copying sidecar to Tauri binaries folder...
echo -------------------------------------------------------
if not exist "frontend\src-tauri\binaries" mkdir "frontend\src-tauri\binaries"
copy /Y "dist\server.exe" "frontend\src-tauri\binaries\server-x86_64-pc-windows-msvc.exe"
if errorlevel 1 (
    echo ERROR: Failed to copy server.exe to binaries folder.
    pause
    exit /b 1
)
echo Copied server.exe -> frontend\src-tauri\binaries\server-x86_64-pc-windows-msvc.exe

echo.
echo [3/3] Building Tauri app...
echo -------------------------------------------------------
cd frontend
call npm run tauri -- build
if errorlevel 1 (
    echo ERROR: Tauri build failed.
    cd ..
    pause
    exit /b 1
)
cd ..

echo.
echo ============================================================
echo  Build complete!
echo  Installer: frontend\src-tauri\target\release\bundle\
echo ============================================================
pause
