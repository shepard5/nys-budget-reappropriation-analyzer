@echo off
REM Double-click to launch the NYS Reappropriation Automator (Windows).
REM First run: creates a local venv, installs dependencies (~60s).
REM Subsequent runs: starts immediately.

cd /d "%~dp0"

REM Locate Python 3.9+
where python >nul 2>&1
if errorlevel 1 (
  where py >nul 2>&1
  if errorlevel 1 (
    echo Python 3 is not installed.
    echo.
    echo Please install Python 3.11 or newer from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
  )
  set PYCMD=py -3
) else (
  set PYCMD=python
)

REM Version check
%PYCMD% -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)"
if errorlevel 1 (
  echo Installed Python is too old. Need 3.9 or newer.
  pause
  exit /b 1
)

REM Create venv on first run
if not exist ".venv_app\Scripts\python.exe" (
  echo First-time setup - installing dependencies ^(this takes about 60 seconds^)...
  %PYCMD% -m venv .venv_app
  .venv_app\Scripts\python.exe -m pip install --upgrade pip --quiet
  .venv_app\Scripts\python.exe -m pip install -r requirements.txt --quiet
  echo Setup complete.
)

REM Open browser after a short delay, then launch Streamlit (foreground)
start "" /B cmd /c "timeout /T 3 /NOBREAK >nul && start http://localhost:8501"
.venv_app\Scripts\python.exe -m streamlit run app.py ^
  --browser.gatherUsageStats false ^
  --server.headless true
