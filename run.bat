@echo off
cd /d "%~dp0"
title Pasport Checker

where py >nul 2>nul
if not errorlevel 1 goto use_py

where python >nul 2>nul
if not errorlevel 1 goto use_python

goto no_python

:use_py
py -3 app.py
goto finished

:use_python
python app.py
goto finished

:no_python
echo.
echo Python ne naiden / Python not found.
echo.
echo Skachaite Python: https://www.python.org/downloads/
echo Pri ustanovke otmette galochku "Add Python to PATH".
echo.
pause
exit /b 1

:finished
if errorlevel 1 goto show_error
exit /b 0

:show_error
echo.
echo Programma zavershilas s oshibkoi. Tekst oshibki vyshe.
echo Podrobnosti - v faile checker.log ryadom s programmoi.
echo.
pause
exit /b 1
