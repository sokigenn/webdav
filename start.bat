@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
set PORT=8765
set PY312=%LOCALAPPDATA%\Programs\Python\Python312\python.exe
set BUNDLED_INSTALLER=%~dp0python-3.12.7-amd64.exe

echo.
echo  WebDAV Downloader v1.0
echo  ==============================
echo.

:: Step 1: Find Python 3.12, install if missing
if not exist "%PY312%" (
    echo  Python 3.12 not found. Installing...
    if exist "%BUNDLED_INSTALLER%" (
        echo  Using bundled installer...
        "%BUNDLED_INSTALLER%" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_launcher=0
    ) else (
        echo  Bundled installer not found, trying winget...
        winget install --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
    )
    if not exist "%PY312%" (
        echo  [ERROR] Python install failed.
        echo  Please install Python from https://www.python.org/downloads/
        echo  Make sure to check "Add Python to PATH" during install.
        pause & exit /b 1
    )
    echo  Python 3.12 installed.
)

:: Step 2: Create virtual environment
if exist ".venv\Scripts\python.exe" (
    .venv\Scripts\python.exe --version >nul 2>&1
    if errorlevel 1 rmdir /s /q .venv
)
if not exist ".venv\Scripts\python.exe" (
    echo  Creating virtual environment...
    "%PY312%" -m venv .venv
    .venv\Scripts\python.exe --version >nul 2>&1
    if errorlevel 1 (
        echo  [ERROR] Failed to create virtual environment.
        pause & exit /b 1
    )
)

:: Step 3: Install dependencies
echo  Checking dependencies...
.venv\Scripts\python.exe -m pip install -r requirements.txt -q --disable-pip-version-check
if errorlevel 1 (
    echo  [ERROR] Failed to install dependencies. Check your network connection.
    pause & exit /b 1
)

:: Step 4: Start server
if not exist "downloads" mkdir downloads
echo.
echo  Server started!
echo  Open browser: http://localhost:%PORT%
echo  Press Ctrl+C to stop.
echo.
start "" cmd /c "timeout /t 2 /nobreak >nul & start http://localhost:%PORT%"
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%
if errorlevel 1 (
    echo.
    echo  [ERROR] Server stopped unexpectedly.
    pause
)

endlocal
