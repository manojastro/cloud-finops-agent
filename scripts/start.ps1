$ErrorActionPreference = 'Stop'
$ProjectPath = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectPath
python -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8102
