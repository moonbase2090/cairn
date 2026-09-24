# Install a verified cairn Windows archive.
# Usage: powershell -File .\install.ps1 [-Prefix C:\absolute\path]
param(
    [string]$Prefix = (Join-Path $env:USERPROFILE ".local")
)
$ErrorActionPreference = "Stop"
$payload = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not [System.IO.Path]::IsPathRooted($Prefix)) {
    throw "The prefix must be an absolute path."
}
$target = Get-Content (Join-Path $payload "TARGET") -Raw
$target = $target.Trim()
if ($target -ne "x86_64-pc-windows-msvc") {
    throw "This archive is for $target, not Windows x64."
}
Set-Location $payload
Get-Content .\SHA256SUMS | ForEach-Object {
    if ($_ -match '^([0-9a-f]{64})  (.+)$') {
        $hash = (Get-FileHash -Algorithm SHA256 -Path $Matches[2]).Hash.ToLower()
        if ($hash -ne $Matches[1]) { throw "checksum mismatch: $($Matches[2])" }
    }
}
$version = (Get-Content .\VERSION -Raw).Trim()
$dest = Join-Path $Prefix "lib\cairn"
New-Item -ItemType Directory -Force -Path $dest, (Join-Path $Prefix "bin") | Out-Null
foreach ($name in @("python", "app", "bin")) {
    $item = Join-Path $dest $name
    if (Test-Path $item) { Remove-Item -Recurse -Force $item }
    Copy-Item -Recurse (Join-Path $payload $name) $item
}
foreach ($name in @("cairn", "cairn-mcp", "cairn-embedd")) {
    Copy-Item (Join-Path $dest "bin\$name.cmd") (Join-Path $Prefix "bin\$name.cmd") -Force
}
Write-Host "Installed cairn $version. Programs are in $(Join-Path $Prefix 'bin')."
Write-Host "Next, per project: cairn init --yes && cairn bootstrap"
