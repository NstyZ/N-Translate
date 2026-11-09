@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0pysite\sitecustomize.py" set "PYTHONPATH=%~dp0pysite"
if exist "%~dp0.hf" set "HF_HOME=%~dp0.hf"
set "PYTHONIOENCODING=utf-8"

set "VENVPY=%~dp0.venv\Scripts\python.exe"
set "SYS=python"
where py >nul 2>&1
if not errorlevel 1 set "SYS=py -3"

rem ---- ????????venv ?????????? ----
if not exist "%VENVPY%" goto repair
"%VENVPY%" -c "import sys" >nul 2>&1
if errorlevel 1 goto repair
if exist "%~dp0models\opus-en-zh-int8-stateless-ov\openvino_decoder_model.xml" goto run
if exist "%~dp0models\index-translate-2b-gguf\Index-Translate-2B.Q4_K_M.gguf" goto run

:repair
echo ============================================================
echo  Running environment is incomplete.
echo  Starting automatic check + repair (doctor.py --fix) ...
echo ============================================================
%SYS% "%~dp0doctor.py" --fix
if not exist "%VENVPY%" goto setup_failed
"%VENVPY%" -c "import sys" >nul 2>&1
if errorlevel 1 goto setup_failed

:run
echo ============================================================
echo  Local NPU translation server  (EN -^> ZH)
echo  URL  : http://127.0.0.1:8765
echo  NOTE : Keep this window OPEN while using the extension.
echo ============================================================
:loop
"%VENVPY%" "%~dp0server.py" --device NPU --port 8765
if exist "%~dp0restart.flag" (
  del "%~dp0restart.flag" >nul 2>&1
  echo.
  echo [restart] Model changed - restarting with the new model...
  timeout /t 2 /nobreak >nul
  goto loop
)
echo.
echo [EXITED] The server stopped. Read the messages above for the reason.
echo          If this keeps happening, run:  doctor.bat fix
pause
exit /b 0

:setup_failed
echo.
echo [ERR] Automatic repair failed. Please run setup.bat and read the messages,
echo       or see docs/ for troubleshooting.
pause
exit /b 1