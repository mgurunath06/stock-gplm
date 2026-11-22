@echo off
rem Owner 2026-11-22: LLM discovery (15m, then 1d), then the tuning chain. Sequential on purpose: Ollama
rem (qwen3.6, ~27 GB committed) + two worker pools at once exhausted Windows memory. Resumable - rerun to continue.
cd /d H:\Guru\Learning\StockGLPM\app
set PYTHONPATH=src
set PYTHONIOENCODING=utf-8
set PY=E:\Guru\Learning\Alekhya\venv\Scripts\python.exe
set LOG=H:\Guru\Learning\StockGLPM\app\reports\llm\discover.log
if not exist reports\llm mkdir reports\llm
for %%T in (15m 1d) do (
  echo ==== %date% %time% discover %%T >> "%LOG%"
  %PY% -m stock_gplm.app.discover --tf %%T --workers 16 >> "%LOG%" 2>&1
)
echo ==== %date% %time% discovery done, starting tuning chain >> "%LOG%"
call H:\Guru\Learning\StockGLPM\app\deploy\windows\tune_phase1.cmd
