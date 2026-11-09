@echo off
rem Launched by Chrome as the Native Messaging host.
rem stdout is the protocol channel: never write anything else to it.
setlocal
cd /d "%~dp0"
if exist "%~dp0pysite\sitecustomize.py" set "PYTHONPATH=%~dp0pysite"
if exist "%~dp0.hf" set "HF_HOME=%~dp0.hf"
set "PYTHONIOENCODING=utf-8"

rem Prefer the bundled venv (windowless pythonw).
if exist "%~dp0.venv\Scripts\pythonw.exe" (
  "%~dp0.venv\Scripts\pythonw.exe" "%~dp0npu_host.py"
  exit /b 0
)

rem Fallback: runtime not installed yet. The host still has to answer, otherwise
rem the extension only sees "host exited" and cannot tell the user what to do.
where py >nul 2>&1
if not errorlevel 1 (
  py -3 "%~dp0npu_host.py"
  exit /b 0
)
python "%~dp0npu_host.py"