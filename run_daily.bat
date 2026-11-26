@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
python "%~dp0run_daily.py" --config "%~dp0config.ini" %*
set "review_exit=%errorlevel%"
if "%review_exit%"=="0" exit /b 0
echo.
echo FAB SOP review exited with code %review_exit%. See the error above.
pause
exit /b %review_exit%
