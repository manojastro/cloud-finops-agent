$ErrorActionPreference = 'Stop'
$ProjectPath = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectPath
python -m pytest
