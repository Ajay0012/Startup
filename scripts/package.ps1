$ErrorActionPreference = 'Stop'
& .\.venv\Scripts\python.exe -m build
dotnet publish apps/session-agent/Pangu.SessionAgent.csproj -c Release -r win-x64 --self-contained false -o dist/session-agent
dotnet publish apps/overlay-host/Pangu.OverlayHost.csproj -c Release -r win-x64 --self-contained false -o dist/overlay-host
Write-Warning 'Python executable and installer require their optional build tools; see docs/KNOWN_LIMITATIONS.md.'
