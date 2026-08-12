param(
    [string]$ExpectedSha256 = $env:PANGU_SPEAKER_MODEL_SHA256,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$targetDir = Join-Path $root 'models\voice\speaker'
$modelName = '3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx'
$target = Join-Path $targetDir $modelName
$url = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/$modelName"
$temp = Join-Path $env:TEMP 'pangu-owner-speaker-model.onnx'

if ((Test-Path -LiteralPath $target) -and -not $Force) {
    Write-Output 'ALREADY_INSTALLED'
    exit 0
}

New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue

try {
    Invoke-WebRequest -Uri $url -OutFile $temp -UseBasicParsing
    if ((Get-Item -LiteralPath $temp).Length -lt 1000000) {
        throw 'SPEAKER_MODEL_DOWNLOAD_INVALID'
    }
    if (-not [string]::IsNullOrWhiteSpace($ExpectedSha256)) {
        if ($ExpectedSha256 -notmatch '^[0-9a-fA-F]{64}$') {
            throw 'PANGU_SPEAKER_MODEL_SHA256 must be a 64-character SHA-256 when provided.'
        }
        $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $temp).Hash.ToLowerInvariant()
        if ($actual -ne $ExpectedSha256.ToLowerInvariant()) {
            throw "SPEAKER_MODEL_CHECKSUM_MISMATCH expected=$ExpectedSha256 actual=$actual"
        }
    }
    Move-Item -LiteralPath $temp -Destination $target -Force
    Write-Output 'INSTALLED'
}
finally {
    Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
}
