@echo off
REM ============================================================
REM  build_tauri.bat  —  Full production build
REM
REM  Prerequisites (run once):
REM    pip install pyinstaller fastapi uvicorn pydantic
REM
REM  Output:
REM    frontend\src-tauri\target\release\bundle\
REM      nsis\SteamHighlightExtractor_2.1.0_x64-setup.exe  (installer)
REM      msi\SteamHighlightExtractor_2.1.0_x64_en-US.msi   (MSI)
REM ============================================================

setlocal

echo.
echo  Steam Highlight Extractor — Production Build
echo  ============================================
echo.

REM ── Check prerequisites ───────────────────────────────────────────
where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python and try again.
    pause & exit /b 1
)

where pyinstaller >nul 2>&1
if errorlevel 1 (
    echo ERROR: PyInstaller not found.
    echo   Run:  pip install pyinstaller fastapi uvicorn pydantic
    pause & exit /b 1
)

where cargo >nul 2>&1
if errorlevel 1 (
    echo ERROR: Rust/Cargo not found. Install from https://rustup.rs
    pause & exit /b 1
)

REM ── Step 1: Build Python server ───────────────────────────────────
echo [1/4] Building Python server sidecar...
echo -----------------------------------------------
pyinstaller server.spec --noconfirm --clean
if errorlevel 1 (
    echo ERROR: PyInstaller build failed.
    pause & exit /b 1
)
echo.

REM ── Step 2: Copy sidecar to Tauri binaries ────────────────────────
echo [2/4] Installing sidecar binary...
echo -----------------------------------------------
if not exist "frontend\src-tauri\binaries" mkdir "frontend\src-tauri\binaries"
copy /Y "dist\server.exe" "frontend\src-tauri\binaries\server-x86_64-pc-windows-msvc.exe"
if errorlevel 1 (
    echo ERROR: Could not copy server.exe to binaries folder.
    pause & exit /b 1
)
echo   OK: server-x86_64-pc-windows-msvc.exe
echo.

REM ── Step 3: Build frontend ────────────────────────────────────────
echo [3/4] Building Svelte frontend...
echo -----------------------------------------------
cd frontend
call npm run build
if errorlevel 1 (
    echo ERROR: Frontend build failed.
    cd .. & pause & exit /b 1
)
echo.

REM ── Step 4: Build Tauri app with sidecar enabled ──────────────────
echo [4/4] Building Tauri app (this takes a few minutes)...
echo -----------------------------------------------
REM Pass externalBin as a config override — keeps tauri.conf.json clean for dev
call npx tauri build --config "{\"bundle\":{\"externalBin\":[\"binaries/server\"]}}"
if errorlevel 1 (
    echo ERROR: Tauri build failed.
    cd .. & pause & exit /b 1
)
cd ..

echo.
echo  ============================================================
echo   Build complete!
echo.
echo   Installer (NSIS):
echo     frontend\src-tauri\target\release\bundle\nsis\
echo   MSI installer:
echo     frontend\src-tauri\target\release\bundle\msi\
echo  ============================================================
echo.
pause
