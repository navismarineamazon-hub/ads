@echo off
REM Daily scheduled task: python run.py suggest (read-only, no account writes).
REM Output appended to output\scheduled_run.log for later inspection.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo. >> output\scheduled_run.log
echo ===== %date% %time% ===== >> output\scheduled_run.log
E:\Python\python.exe run.py suggest >> output\scheduled_run.log 2>&1
