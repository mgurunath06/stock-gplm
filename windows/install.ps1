<#
.SYNOPSIS
  Install the Stock GPLM research application on native Windows (Python 3.12 venv, optional CUDA GPU).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File windows\install.ps1 -DataRoot C:\GPLM\data\raw -Gpu

.PARAMETER DataRoot
  Folder holding the raw Shoonya tree (the folder that contains 'shoonya\NSE' and 'shoonya-minute\NSE').
  Writes config\app\gplm.windows.json pointing at it. Raw files are only ever read.
.PARAMETER StateDir
  State folder for the database, models, caches and reports (default C:\GPLM\state).
.PARAMETER Gpu
  Also install a CUDA build of PyTorch (cu128 wheels for RTX 50-series) and run the GPU self-test.
.PARAMETER CudaIndex
  PyTorch wheel index (default https://download.pytorch.org/whl/cu128).
#>
param(
  [string]$DataRoot = "",
  [string]$StateDir = "C:\GPLM\state",
  [switch]$Gpu,
  [string]$CudaIndex = "https://download.pytorch.org/whl/cu128",
  [string]$Python = ""
)
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
Write-Host "Stock GPLM install - repository $Repo"

# 1. Python 3.12
if (-not $Python) {
  $py = Get-Command py -ErrorAction SilentlyContinue
  if ($py) { $Python = (& py -3.12 -c "import sys; print(sys.executable)") 2>$null }
}
if (-not $Python) { $Python = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $Python) { throw "Python 3.12 not found. Install it from https://www.python.org/downloads/ (tick 'Add to PATH'), then re-run." }
$ver = & $Python -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ($ver -ne "3.12") { throw "Python 3.12 is required; found $ver at $Python. Use -Python <path to python 3.12>." }
Write-Host "Python: $Python ($ver)"

# 2. Virtual environment
$Venv = Join-Path $Repo ".venv"
if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) {
  & $Python -m venv $Venv
}
$VPy = Join-Path $Venv "Scripts\python.exe"
& $VPy -m pip install --upgrade pip
& $VPy -m pip install -r (Join-Path $Repo "requirements\app.txt")

# 3. Optional GPU stack
if ($Gpu) {
  $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
  if (-not $smi) { Write-Warning "nvidia-smi not found: install the current NVIDIA Game Ready/Studio driver first." }
  else { & nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv }
  & $VPy -m pip install -r (Join-Path $Repo "requirements\gpu-cu128.txt") --index-url $CudaIndex
}

# 4. Windows config (data and state paths)
if ($DataRoot) {
  $cfgPath = Join-Path $Repo "config\app\gplm.json"
  $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
  $cfg.home = $StateDir
  $cfg.data.daily_root = (Join-Path $DataRoot "shoonya\NSE")
  $cfg.data.minute_roots = @((Join-Path $DataRoot "shoonya\NSE\minute"), (Join-Path $DataRoot "shoonya-minute\NSE"))
  $cfg.data.real_raw_tree = $DataRoot
  $cfg.discovery.engine_config = (Join-Path $Repo "config\m54\unified-default.json")
  $out = Join-Path $Repo "config\app\gplm.windows.json"
  # UTF-8 without BOM (Windows PowerShell 5.1 adds a BOM with -Encoding UTF8; GPLM tolerates both)
  [System.IO.File]::WriteAllText($out, ($cfg | ConvertTo-Json -Depth 20), (New-Object System.Text.UTF8Encoding($false)))
  Write-Host "Wrote $out (gplm.cmd uses it automatically)"
}

# 5. Self-checks
$Gplm = Join-Path $Repo "windows\gplm.cmd"
& $Gplm doctor
if ($Gpu) {
  & $Gplm gpu-test --require-gpu
  if ($LASTEXITCODE -ne 0) { Write-Warning "GPU self-test did not pass. See the message above; the CPU path still works (--device cpu)." }
}
Write-Host ""
Write-Host "Done. Evening run:   windows\gplm.cmd daily --stocks RELIANCE,HDFCBANK,ICICIBANK --device auto"
Write-Host "Dashboard:           windows\gplm.cmd dashboard --open"
