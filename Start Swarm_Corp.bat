@echo off
setlocal
set "ROOT=%~dp0"

echo ============================================================
echo Swarm_Corp - multi-provider coding swarm
echo ============================================================

cd /d "%ROOT%"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo No virtual environment found at .venv\ - run setup first:
    echo   python -m venv .venv
    echo   .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

if not exist ".env" (
    echo.
    echo No .env found. Copy .env.example to .env and paste your API keys in first.
    pause
    exit /b 1
)

echo.
set /p TASK="What should the swarm build? "
if "%TASK%"=="" (
    echo No task entered, exiting.
    pause
    exit /b 1
)

echo.
".venv\Scripts\python.exe" swarm_corp.py "%TASK%"

echo.
if errorlevel 1 (
    echo Swarm did not approve after all rounds - check swarm_output\ for the
    echo transcript, then bring it to Claude Code as the escalation lane.
) else (
    echo Done - see swarm_output\ for the generated code and transcript.
)
pause
