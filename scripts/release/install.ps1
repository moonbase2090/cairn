# Install a verified cairn Windows archive.
# Usage: powershell -File .\install.ps1 [-Prefix C:\absolute\path] [-NoAgentSkills]
param(
    [string]$Prefix = (Join-Path $env:USERPROFILE ".local"),
    [switch]$NoAgentSkills
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
if ($NoAgentSkills -or $env:CAIRN_NO_AGENT_SKILLS -eq "1") {
    Write-Host "Skipped automatic Agent Skill installation. Run CAIRN_EXPERIMENTAL_SKILLS=1 cairn skills install --agent detected later."
} else {
    $previousSkillGate = $env:CAIRN_EXPERIMENTAL_SKILLS
    $env:CAIRN_EXPERIMENTAL_SKILLS = "1"
    try {
        & (Join-Path $dest "bin\cairn.cmd") skills install --agent detected
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Cairn installed, but automatic Agent Skill installation failed. Retry with cairn skills install --agent detected, or set CAIRN_NO_AGENT_SKILLS=1 to skip it."
        }
    } finally {
        if ($null -eq $previousSkillGate) {
            Remove-Item Env:CAIRN_EXPERIMENTAL_SKILLS -ErrorAction SilentlyContinue
        } else {
            $env:CAIRN_EXPERIMENTAL_SKILLS = $previousSkillGate
        }
    }
}
Write-Host "Installed cairn $version. Programs are in $(Join-Path $Prefix 'bin')."
Write-Host "Next, per project: cairn init --yes && cairn bootstrap"
