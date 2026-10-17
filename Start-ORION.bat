@echo off
setlocal
cd /d "%~dp0"
title ORION

rem ---------------------------------------------------------------
rem  Starts ORION. Double-click this file.
rem  It uses the project's own Python (.venv), picks a free port,
rem  and opens your browser when ORION is ready.
rem ---------------------------------------------------------------

set "VENV_PY=%~dp0.venv\Scripts\python.exe"

if exist "%VENV_PY%" goto run

echo.
echo  ORION's Python environment (the .venv folder) was not found.
echo.
choice /c YN /m "Set it up now? This downloads ORION's packages, so you need internet"
if errorlevel 2 goto manual

set "SYS_PY="
where py >nul 2>&1 && set "SYS_PY=py -3"
if not defined SYS_PY where python >nul 2>&1 && set "SYS_PY=python"
if not defined SYS_PY goto nopython

echo.
echo  Creating the environment...
%SYS_PY% -m venv .venv
if errorlevel 1 goto failed

echo.
echo  Installing packages (this can take a few minutes)...
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 goto failed

:run
"%VENV_PY%" launch.py %*
if errorlevel 1 goto failed
goto end

:manual
echo.
echo  To set it up yourself, open PowerShell in this folder and run:
echo      py -3 -m venv .venv
echo      .\.venv\Scripts\python -m pip install -r requirements.txt
echo.
pause
exit /b 1

:nopython
echo.
echo  Python was not found on this computer.
echo  Install Python 3.10 or newer from https://www.python.org/downloads/
echo  (tick "Add python.exe to PATH" in the installer), then run this again.
echo.
pause
exit /b 1

:failed
echo.
echo  ORION stopped because of an error. Read the messages above.
echo.
pause
exit /b 1

:end
exit /b 0
