@echo off
setlocal
call "%~dp0windows\gplm.cmd" gpu-evidence %*
exit /b %ERRORLEVEL%
