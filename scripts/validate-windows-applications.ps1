param([ValidateSet('Discover','ResolveNotepad','OpenNotepad','CloseNotepad')][string]$Check = 'Discover')
$ErrorActionPreference = 'Stop'
if (-not [Environment]::UserInteractive) { throw 'Interactive Windows session required.' }
$out = Join-Path $PSScriptRoot '..\.test-runtime\validation'; New-Item -ItemType Directory -Force -Path $out | Out-Null
$report = [ordered]@{ check=$Check; passed=$false; timestamp=(Get-Date).ToUniversalTime().ToString('o') }
try {
  if ($Check -eq 'Discover') { python -m pangu apps discover }
  elseif ($Check -eq 'ResolveNotepad') { python -m pangu apps resolve Notepad }
  elseif ($Check -eq 'OpenNotepad') { python -m pangu apps open Notepad }
  elseif ($Check -eq 'CloseNotepad') { Write-Host 'Confirm Notepad has no unsaved content before continuing.'; $ok = Read-Host 'Type YES'; if ($ok -ne 'YES') { throw 'Confirmation declined' }; python -m pangu apps close Notepad }
  $report.passed = $true
} catch { $report.error = $_.Exception.Message; throw }
finally { $report | ConvertTo-Json | Set-Content (Join-Path $out "application-validation-$Check.json") }
