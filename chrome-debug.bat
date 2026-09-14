@echo off
cd /d "%~dp0"
title Chrome dlya Pasport Checker

set "CHROME="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" set "CHROME=%LocalAppData%\Google\Chrome\Application\chrome.exe"
if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" if "%CHROME%"=="" set "CHROME=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" if "%CHROME%"=="" set "CHROME=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"

if "%CHROME%"=="" goto no_chrome

echo Zapuskayu vash brauzer dlya Pasport Checker.
echo.
echo 1. V otkryvshemsya okne proidite proverku "ya ne robot".
echo 2. Ne zakryvaite eto okno brauzera.
echo 3. V programme postavte galochku "Ispolzovat svoi Chrome" i nazhmite
echo    "Proverit seichas".
echo.

start "" "%CHROME%" --remote-debugging-port=9222 --user-data-dir="%~dp0chrome-profile" "https://warszawa.pasport.org.ua/solutions/e-queue"
exit /b 0

:no_chrome
echo.
echo Chrome i Edge ne naideny v standartnykh papkakh.
echo Ukazhite put k brauzeru vruchnuyu v faile config.json (pole browser_path).
echo.
pause
exit /b 1
