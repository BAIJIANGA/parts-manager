@echo off
chcp 65001 >nul
setlocal
title Parts Manager
cd /d "%~dp0.."
set "ROOT=%CD%"

rem ---- pick an interpreter: project venv first, then py launcher, then PATH ----
set "PYEXE="
set "PYARGS="
if exist "%ROOT%\venv\Scripts\python.exe" set "PYEXE=%ROOT%\venv\Scripts\python.exe"
if not defined PYEXE ( where py >nul 2>nul && set "PYEXE=py" && set "PYARGS=-3" )
if not defined PYEXE ( where python >nul 2>nul && set "PYEXE=python" )
if not defined PYEXE (
  echo.
  echo  [ERROR] No Python found.
  echo  Install Python 3.10+ from https://www.python.org/downloads/
  echo  and tick "Add python.exe to PATH", or keep the venv folder.
  echo.
  pause
  exit /b 1
)

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

rem ---- open the browser ~2s later, so the server is already listening ----
start "" /min cmd /c "ping -n 3 127.0.0.1 >nul & start http://127.0.0.1:8000/"

"%PYEXE%" %PYARGS% app\server.py --port 8000

echo.
echo  Server stopped. Press any key to close.
pause >nul
