$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
python run_radar.py @args
