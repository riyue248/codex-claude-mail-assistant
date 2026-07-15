$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$OutputDir = Join-Path $ProjectDir 'dist'
$WorkDir = Join-Path $ProjectDir 'build'
$SpecDir = Join-Path $ProjectDir 'build-spec'

python -m PyInstaller `
  --noconfirm `
  --clean `
  --onefile `
  --noconsole `
  --name 'CodexMailAssistant' `
  --distpath $OutputDir `
  --workpath $WorkDir `
  --specpath $SpecDir `
  (Join-Path $ProjectDir 'codex_email_app.py')

Write-Host "Built: $(Join-Path $OutputDir 'CodexMailAssistant.exe')"
