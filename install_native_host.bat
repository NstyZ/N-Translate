@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0pysite\sitecustomize.py" set "PYTHONPATH=%~dp0pysite"
if exist "%~dp0.hf" set "HF_HOME=%~dp0.hf"
set "PYTHONIOENCODING=utf-8"
echo ============================================================
echo  Register Chrome Native Messaging host (one time)
echo  Lets the extension start the local server by itself.
echo ============================================================
echo.
echo Tip: to pin your exact extension ID, pass it as an argument:
echo      install_native_host.bat abcdefghijklmnopabcdefghijklmnop
echo.
"%~dp0.venv\Scripts\python.exe" "%~dp0install_native_host.py" %*
echo.
pause
