$ErrorActionPreference = 'Stop'
$target = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\Pangu Session Agent.lnk'
Write-Warning "Create a shortcut to the packaged session agent at: $target"
