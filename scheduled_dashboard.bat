@echo off
REM Independent scheduled task: python run.py dashboard (read-only, no account writes).
REM Refreshes 重叠分析看板.html + dashboard.html on its own schedule, decoupled from
REM scheduled_suggest.bat — run this as infrequently as you like (e.g. once a day)
REM without slowing down the main suggest schedule.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo. >> output\scheduled_run.log
echo ===== %date% %time% (dashboard) ===== >> output\scheduled_run.log
E:\Python\python.exe run.py dashboard >> output\scheduled_run.log 2>&1
