param([switch]$Force)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$manifestPath = Join-Path $root 'models\voice\vad\silero\v4\manifest.json'
if (-not (Test-Path -LiteralPath $manifestPath)) { Write-Output 'MISSING'; exit 3 }
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$target = Split-Path -Parent $manifestPath
$final = Join-Path $target $manifest.filename
New-Item -ItemType Directory -Force -Path $target | Out-Null
function Test-ModelChecksum([string]$Path, [string]$Expected) {
  (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant() -eq $Expected.ToLowerInvariant()
}
if (Test-Path -LiteralPath $final) {
  if (Test-ModelChecksum $final $manifest.sha256) { Write-Output 'ALREADY_INSTALLED'; exit 0 }
  Remove-Item -LiteralPath $final -Force
  if (-not $Force) { Write-Output 'CHECKSUM_MISMATCH'; exit 1 }
}
$partial = "$final.partial"
Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
try { Invoke-WebRequest -Uri $manifest.download_source -OutFile $partial -UseBasicParsing } catch { Remove-Item $partial -Force -ErrorAction SilentlyContinue; Write-Output 'MISSING'; exit 3 }
if (-not (Test-ModelChecksum $partial $manifest.sha256)) { Remove-Item $partial -Force; Write-Output 'CHECKSUM_MISMATCH'; exit 1 }
Move-Item -LiteralPath $partial -Destination $final -Force
Write-Output 'INSTALLED'
