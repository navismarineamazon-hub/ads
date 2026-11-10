@echo off
REM Weekly scheduled task: python run.py weekly-report (read-only, no account writes).
REM Independent of scheduled_suggest.bat/scheduled_dashboard.bat (those stay daily).
REM Register in Windows Task Scheduler with a Weekly trigger, same steps as the
REM existing two daily tasks, just pointed at this .bat instead.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo. >> output\scheduled_run.log
echo ===== %date% %time% (weekly-report) ===== >> output\scheduled_run.log
E:\Python\python.exe run.py weekly-report >> output\scheduled_run.log 2>&1
