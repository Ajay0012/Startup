param(
    [string]$ExpectedSha256 = $env:PANGU_WAKE_ARCHIVE_SHA256,
    [switch]$Force
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$target = Join-Path $root 'models\voice\wake\sherpa-kws'
$archiveUrl = 'https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2'
$archive = Join-Path $env:TEMP 'pangu-sherpa-kws.tar.bz2'
$extract = Join-Path $env:TEMP 'pangu-sherpa-kws-extract'

if ([string]::IsNullOrWhiteSpace($ExpectedSha256) -or $ExpectedSha256 -notmatch '^[0-9a-fA-F]{64}$') {
    Write-Error 'EXPECTED_SHA256_REQUIRED: set PANGU_WAKE_ARCHIVE_SHA256 to a trusted SHA-256 before installation.'
}

if ((Test-Path $target) -and -not $Force) {
    $required = @('encoder.onnx','decoder.onnx','joiner.onnx','tokens.txt','keywords.txt')
    $complete = $true
    foreach ($item in $required) { if (-not (Test-Path (Join-Path $target $item))) { $complete = $false } }
    if ($complete) { Write-Output 'ALREADY_INSTALLED'; exit 0 }
}

Remove-Item $archive -Force -ErrorAction SilentlyContinue
Remove-Item $extract -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $extract | Out-Null
New-Item -ItemType Directory -Force -Path $target | Out-Null

try {
    Invoke-WebRequest -Uri $archiveUrl -OutFile $archive -UseBasicParsing
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant()
    if ($actual -ne $ExpectedSha256.ToLowerInvariant()) {
        throw "WAKE_ARCHIVE_CHECKSUM_MISMATCH expected=$ExpectedSha256 actual=$actual"
    }
    tar -xf $archive -C $extract
    $source = Get-ChildItem $extract -Directory | Select-Object -First 1
    if (-not $source) { throw 'WAKE_ARCHIVE_LAYOUT_INVALID' }

    Copy-Item (Join-Path $source.FullName 'encoder-epoch-13-avg-2-chunk-8-left-64.int8.onnx') (Join-Path $target 'encoder.onnx') -Force
    Copy-Item (Join-Path $source.FullName 'decoder-epoch-13-avg-2-chunk-8-left-64.onnx') (Join-Path $target 'decoder.onnx') -Force
    Copy-Item (Join-Path $source.FullName 'joiner-epoch-13-avg-2-chunk-8-left-64.int8.onnx') (Join-Path $target 'joiner.onnx') -Force
    Copy-Item (Join-Path $source.FullName 'tokens.txt') (Join-Path $target 'tokens.txt') -Force
    Copy-Item (Join-Path $source.FullName 'en.phone') (Join-Path $target 'en.phone') -Force

    Add-Content -LiteralPath (Join-Path $target 'en.phone') -Value "`nPANGU P AE1 NG G UW0`nPANGUU P AE1 NG G UW0`nPANGOO P AE1 NG G UW0"
    @'
HEY PANGU :2.0 #0.28 @HEY_PANGU
PANGU :1.6 #0.32 @PANGU
HAY PANGU :1.4 #0.35 @HAY_PANGU
HEY PANGUU :1.8 #0.30 @HEY_PANGUU
HEY PANGOO :1.8 #0.30 @HEY_PANGOO
'@ | Set-Content -LiteralPath (Join-Path $target 'keywords_raw.txt') -Encoding utf8

    $cli = Get-Command sherpa-onnx-cli -ErrorAction SilentlyContinue
    if (-not $cli) { throw 'SHERPA_ONNX_CLI_UNAVAILABLE: install the project dependencies first.' }
    & $cli.Source text2token `
        --tokens (Join-Path $target 'tokens.txt') `
        --tokens-type phone+ppinyin `
        --lexicon (Join-Path $target 'en.phone') `
        (Join-Path $target 'keywords_raw.txt') `
        (Join-Path $target 'keywords.txt')
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path (Join-Path $target 'keywords.txt'))) {
        throw 'WAKE_KEYWORD_TOKENIZATION_FAILED'
    }

    $manifest = [ordered]@{
        source = $archiveUrl
        source_archive_sha256 = $actual
        model = 'sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20'
        profile = 'chunk-8-int8-low-latency'
        installed_at_utc = [DateTime]::UtcNow.ToString('o')
        artifacts = [ordered]@{}
    }
    foreach ($name in @('encoder.onnx','decoder.onnx','joiner.onnx','tokens.txt','keywords.txt','en.phone')) {
        $manifest.artifacts[$name] = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $target $name)).Hash.ToLowerInvariant()
    }
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $target 'manifest.json') -Encoding utf8
    Write-Output 'INSTALLED'
}
finally {
    Remove-Item $archive -Force -ErrorAction SilentlyContinue
    Remove-Item $extract -Recurse -Force -ErrorAction SilentlyContinue
}
