@echo off
setlocal
rem Feishu work-log desktop widget launcher (hidden, auto-retry up to 3 times)
if defined MIN goto run
set MIN=1
start /min cmd /c ""%~f0""
exit /b
:run
cd /d "C:\Users\Administrator\WorkBuddy\2026-11-04-11-23-20\feishu-log-widget"
set N=0
:try
start "" "C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe" app.py
rem ping-based sleep (timeout.exe fails when input is redirected)
ping -n 7 127.0.0.1 >nul
rem tasklist piped output is UTF-16 so find can't match it; use PowerShell instead
powershell -NoProfile -Command "if (Get-Process -Name pythonw -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if not errorlevel 1 goto done
set /a N=N+1
echo %date% %time% launcher retry %N% >> data\life.log
if %N% geq 3 goto done
goto try
:done
exit
