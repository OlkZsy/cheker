@echo off
cd /d "%~dp0"
title Pasport Checker - diagnostika

where py >nul 2>nul
if not errorlevel 1 goto use_py

where python >nul 2>nul
if not errorlevel 1 goto use_python

echo.
echo Python ne naiden / Python not found.
echo Skachaite Python: https://www.python.org/downloads/
echo.
pause
exit /b 1

:use_py
py -3 diagnose.py
goto finished

:use_python
python diagnose.py

:finished
if errorlevel 1 pause
exit /b 0
