@echo off
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "VENVPY=%~dp0.venv\Scripts\python.exe"
set "SYS=python"
where py >nul 2>&1
if not errorlevel 1 set "SYS=py -3"

if /i "%~1"=="fix" goto fix
if /i "%~1"=="check" goto check
goto help

:check
if exist "%VENVPY%" (
  "%VENVPY%" "%~dp0doctor.py"
) else (
  %SYS% "%~dp0doctor.py"
)
goto end

:fix
echo Repairing runtime environment (may take a few minutes on first run) ...
if exist "%VENVPY%" (
  "%VENVPY%" "%~dp0doctor.py" --fix %2 %3 %4
) else (
  %SYS% "%~dp0doctor.py" --fix %2 %3 %4
)
goto end

:help
echo Usage:
echo   doctor.bat          check the runtime environment
echo   doctor.bat fix      check AND auto-repair what is missing
echo   doctor.bat fix model   also download the main model (1.2GB)
echo.
if exist "%VENVPY%" (
  "%VENVPY%" "%~dp0doctor.py"
) else (
  %SYS% "%~dp0doctor.py"
)

:end
echo.
pause