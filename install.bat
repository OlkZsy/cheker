@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Установка Pasport Checker

echo Ставлю Playwright и браузер Chromium. Это займёт несколько минут...
echo.

where py >nul 2>nul
if %errorlevel%==0 (set PY=py -3) else (set PY=python)

%PY% -m pip install --upgrade pip
%PY% -m pip install -r requirements.txt
%PY% -m playwright install chromium

echo.
echo Готово. Запускайте программу файлом run.bat
pause
