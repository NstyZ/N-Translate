@echo off
rem Launched by Chrome as the Native Messaging host.
rem stdout is the protocol channel: never write anything else to it.
setlocal
cd /d "%~dp0"
if exist "%~dp0pysite\sitecustomize.py" set "PYTHONPATH=%~dp0pysite"
if exist "%~dp0.hf" set "HF_HOME=%~dp0.hf"
set "PYTHONIOENCODING=utf-8"
"%~dp0.venv\Scripts\pythonw.exe" "%~dp0npu_host.py"
