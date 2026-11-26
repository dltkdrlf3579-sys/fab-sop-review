@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
python run_daily.py --config "%~dp0config.ini" %*
exit /b %errorlevel%
