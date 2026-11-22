@echo off
rem LLM fine-tuning phase 1 (owner 2026-11-22): daily + intraday. Self-chaining, resumable - rerun to continue.
rem Order: health check -> local LLM (1d, 15m, 5m, 60m) -> API LLM (same order). Log: tune_phase1.log
cd /d H:\Guru\Learning\StockGLPM\app
set PYTHONPATH=src
set PYTHONIOENCODING=utf-8
set PY=E:\Guru\Learning\Alekhya\venv\Scripts\python.exe
set LOG=H:\Guru\Learning\StockGLPM\app\reports\llm\tune_phase1.log
if not exist reports\llm mkdir reports\llm

echo ==== %date% %time% health >> "%LOG%"
%PY% -m stock_gplm.llm.health --model ollama=qwen3.6:35b-a3b >> "%LOG%" 2>&1

for %%T in (1d 15m 5m 60m) do (
  echo ==== %date% %time% local %%T >> "%LOG%"
  %PY% -m stock_gplm.app.tune --tf %%T --name tune-%%T --phase local >> "%LOG%" 2>&1
)
for %%T in (1d 15m 5m 60m) do (
  echo ==== %date% %time% api %%T >> "%LOG%"
  %PY% -m stock_gplm.app.tune --tf %%T --name tune-%%T --phase api >> "%LOG%" 2>&1
)
echo ==== %date% %time% done >> "%LOG%"
echo done > reports\llm\tune_phase1.done
