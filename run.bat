@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Pasport Checker

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 app.py
) else (
    python app.py
)

if errorlevel 1 (
    echo.
    echo Программа завершилась с ошибкой.
    echo Если Python не установлен - скачайте его с https://www.python.org/downloads/
    echo При установке отметьте галочку "Add Python to PATH".
    pause
)
