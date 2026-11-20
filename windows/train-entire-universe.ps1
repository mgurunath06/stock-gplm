$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
$env:PYTHONPATH = 'src'
$env:OPENBLAS_NUM_THREADS = '1'
$env:OMP_NUM_THREADS = '1'
# Separate output home avoids contention with the live research feeder.
# No refresh: reads existing local candles only. Checkpoints support resuming.
& .\.venv\Scripts\python.exe -m stock_gplm.app daily --config config/app/gplm.windows.json --home H:/Guru/Learning/StockGLPM/state-gpu-universe --universe all --device gpu --no-intraday
exit $LASTEXITCODE
