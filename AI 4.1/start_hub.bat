@echo off
setlocal enabledelayedexpansion
title Enterprise AI Hub Startup
color 0b

:: Always run from the folder this .bat file lives in, not wherever it was launched from
cd /d "%~dp0"

echo ===================================================
echo     Enterprise AI Hub - Automated Startup
echo ===================================================
echo.

:: [0/4] Make sure Python is actually installed
echo [0/4] Checking for Python...
where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] Python was not found on this device.
    echo Install Python 3.10+ from https://www.python.org/downloads/
    echo IMPORTANT: on the installer's first screen, tick "Add python.exe to PATH".
    echo Then re-run this file.
    echo.
    pause
    exit /b 1
)
python --version
echo.

:: [1/4] Install/update required libraries, with errors visible
echo [1/4] Verifying and installing required libraries...
python -m pip install --upgrade pip
if exist requirements.txt (
    python -m pip install -r requirements.txt
) else (
    python -m pip install fastapi uvicorn requests pydantic python-multipart python-dotenv
)
if errorlevel 1 (
    echo.
    echo [ERROR] Library install failed. Check your internet connection and
    echo the error message above, then re-run this file.
    echo.
    pause
    exit /b 1
)
echo Libraries are ready!
echo.

:: [2/4] Make sure the app file is actually here before trying to run it
echo [2/4] Checking for ui_app.py...
if not exist "ui_app.py" (
    echo.
    echo [ERROR] ui_app.py was not found in:
    echo %cd%
    echo Make sure this .bat file sits in the same folder as ui_app.py.
    echo.
    pause
    exit /b 1
)
echo Found ui_app.py
echo.

:: Create the .env settings file on first run, with a random APP_SECRET filled in
if not exist ".env" (
    if exist ".env.example" (
        echo Creating .env settings file with a new random APP_SECRET...
        python -c "import secrets,io; t=io.open('.env.example',encoding='utf-8').read().replace('APP_SECRET=\n','APP_SECRET='+secrets.token_hex(32)+'\n',1); io.open('.env','w',encoding='utf-8').write(t)"
        echo.
    )
)

:: Who can open the app: 127.0.0.1 = only this PC (safe default).
:: To let other PCs on your network connect, run:  set HUB_HOST=0.0.0.0   before this file.
if not defined HUB_HOST set HUB_HOST=127.0.0.1

:: [3/4] Start the server in the background, THEN wait for it, THEN open the browser
echo [3/4] Booting up the AI Server (host %HUB_HOST%)...
start "Enterprise AI Hub Server" cmd /k "python -m uvicorn ui_app:app --host %HUB_HOST% --port 8000"

echo Waiting for the server to come online...
timeout /t 4 /nobreak >nul

echo [4/4] Launching web browser...
start "" http://localhost:8000
echo.

echo ===================================================
echo The AI Hub is running in a separate window.
echo Keep that window open while using the AI.
echo Close that window (or press CTRL+C inside it) to stop the server.
echo ===================================================
echo.
pause