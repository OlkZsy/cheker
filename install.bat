@echo off
cd /d "%~dp0"
title Pasport Checker - ustanovka

where py >nul 2>nul
if not errorlevel 1 goto use_py

where python >nul 2>nul
if not errorlevel 1 goto use_python

echo.
echo Python ne naiden / Python not found.
echo Skachaite Python: https://www.python.org/downloads/
echo Pri ustanovke otmette galochku "Add Python to PATH".
echo.
pause
exit /b 1

:use_py
set "PYCMD=py -3"
goto install

:use_python
set "PYCMD=python"
goto install

:install
echo Ustanavlivayu Playwright i brauzer Chromium.
echo Eto zaimet neskolko minut...
echo.
%PYCMD% -m pip install --upgrade pip
%PYCMD% -m pip install -r requirements.txt
%PYCMD% -m playwright install chromium
if errorlevel 1 goto failed

echo.
echo Gotovo. Zapuskaite programmu failom run.bat
echo.
pause
exit /b 0

:failed
echo.
echo Ustanovka ne udalas. Tekst oshibki vyshe.
echo.
pause
exit /b 1
