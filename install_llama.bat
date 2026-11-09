@echo off
REM Download llama.cpp runtime (required by the Index-Translate model)
REM   install_llama.bat          -> CPU build  (about 24 MB, works everywhere)
REM   install_llama.bat cuda     -> CUDA build (about 700 MB, needs NVIDIA GPU)
setlocal
cd /d "%~dp0"
set FLAVOR=%~1
if "%FLAVOR%"=="" set FLAVOR=cpu
if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" "%~dp0install_llama.py" --flavor %FLAVOR%
) else (
  py "%~dp0install_llama.py" --flavor %FLAVOR%
)
echo.
pause