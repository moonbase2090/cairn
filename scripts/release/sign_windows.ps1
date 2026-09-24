# Authenticode-sign Windows executables when a certificate is provided.
# Unsigned builds are recorded in SIGNING.txt.
param([Parameter(Mandatory = $true)][string]$Root)
$ErrorActionPreference = "Stop"
$pfx = $env:WINDOWS_CERT_BASE64
$password = $env:WINDOWS_CERT_PASSWORD
if (-not $pfx) {
    Set-Content -Path (Join-Path $Root "SIGNING.txt") -Value "unsigned"
    exit 0
}
$certPath = Join-Path $env:TEMP "cairn-codesign.pfx"
[IO.File]::WriteAllBytes($certPath, [Convert]::FromBase64String($pfx))
$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($certPath, $password)
Get-ChildItem -Recurse -File $Root -Include *.exe, *.dll | ForEach-Object {
    Set-AuthenticodeSignature -FilePath $_.FullName -Certificate $cert | Out-Null
}
Remove-Item -Force $certPath
Set-Content -Path (Join-Path $Root "SIGNING.txt") -Value "signed"
