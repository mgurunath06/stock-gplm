@echo off
rem Stock GPLM research application launcher for native Windows.
setlocal
set "GPLM_REPO=%~dp0.."
if not defined GPLM_PYTHON set "GPLM_PYTHON=%GPLM_REPO%\.venv\Scripts\python.exe"
if not exist "%GPLM_PYTHON%" (
  echo Python venv not found at %GPLM_PYTHON%. Run: powershell -ExecutionPolicy Bypass -File windows\install.ps1
  exit /b 1
)
if not defined GPLM_CONFIG if exist "%GPLM_REPO%\config\app\gplm.windows.json" set "GPLM_CONFIG=%GPLM_REPO%\config\app\gplm.windows.json"
set "PYTHONPATH=%GPLM_REPO%\src;%PYTHONPATH%"
set "PYTHONUTF8=1"
"%GPLM_PYTHON%" -m stock_gplm.app.cli %*
exit /b %ERRORLEVEL%
