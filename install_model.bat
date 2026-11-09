@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0pysite\sitecustomize.py" set "PYTHONPATH=%~dp0pysite"
if exist "%~dp0.hf" set "HF_HOME=%~dp0.hf"
set "PYTHONIOENCODING=utf-8"
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo [ERR] Python environment not found. Run setup.bat first.
  pause
  exit /b 1
)
if "%~1"=="" (
  echo Usage: install_model.bat ^<model-id^>
  echo.
  "%~dp0.venv\Scripts\python.exe" "%~dp0install_model.py" --list
) else (
  echo Installing model: %1   ^(download + export, may take 10-30 min^)
  "%~dp0.venv\Scripts\python.exe" "%~dp0install_model.py" --id %1
)
echo.
pause
