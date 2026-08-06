param([switch]$Force)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$target = Join-Path $root 'models\voice'
New-Item -ItemType Directory -Force -Path "$target\kws", "$target\vad", "$target\whisper", "$target\manifests" | Out-Null
Write-Host 'Voice model directories are ready. Download URLs and SHA-256 hashes must be declared in a manifest before installation.'
Write-Host 'No model artifact was downloaded automatically.'
