@echo off
rem Native-Windows GPU acceptance: environment check, CUDA self-test with CPU comparison, then real GPU work
rem (batched rule screen and model training on real data). Evidence JSON goes to <home>\reports\gpu-test-*.json
rem and every step is recorded in the audit database.
setlocal
set "G=%~dp0gplm.cmd"
call "%G%" doctor
call "%G%" gpu-test --require-gpu
if errorlevel 1 (
  echo GPU self-test FAILED or CUDA unavailable. Fix the reported cause, then re-run this script.
  exit /b 2
)
call "%G%" discover --stocks RELIANCE --data all-local --timeframes 1d,15m,5m --budget deep --device gpu --skip-engine
call "%G%" replay --stocks RELIANCE --device gpu --max-steps 5 --home "%TEMP%\gplm-gpu-verify"
call "%G%" audit --run
echo.
echo GPU verification finished. Keep the gpu-test JSON and the audit output above as evidence.
