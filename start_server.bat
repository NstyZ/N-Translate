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
echo ============================================================
echo  Local NPU translation server  (EN -^> ZH)
echo  URL  : http://127.0.0.1:8765
echo  NOTE : Keep this window OPEN while using the extension.
echo ============================================================
:loop
"%~dp0.venv\Scripts\python.exe" "%~dp0server.py" --device NPU --port 8765
if exist "%~dp0restart.flag" (
  del "%~dp0restart.flag" >nul 2>&1
  echo.
  echo [restart] Model changed - restarting with the new model...
  timeout /t 2 /nobreak >nul
  goto loop
)
echo.
echo [EXITED] The server stopped. Read the messages above for the reason.
pause
