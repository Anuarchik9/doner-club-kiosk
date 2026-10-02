@echo off
setlocal
cd /d "%~dp0"

if not exist ".env" (
  copy ".env.example" ".env" >nul
)

where py >nul 2>nul
if %errorlevel%==0 (
  set "PY=py -3"
) else (
  set "PY=python"
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  %PY% -m venv .venv
  if errorlevel 1 goto :fail
)

echo Installing dependencies...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :fail

echo.
echo Starting Doner Club Kaspi Bridge...
echo Open http://127.0.0.1:8765 in your browser.
echo.
".venv\Scripts\python.exe" bridge.py
goto :eof

:fail
echo.
echo Setup failed. Make sure Python 3 is installed.
pause
exit /b 1
