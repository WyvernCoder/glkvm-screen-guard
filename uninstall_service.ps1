# Stops and removes the GLKVM Screen Guard Windows service.
# Run in an elevated PowerShell.  User config/logs/snapshots are preserved.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSCommandPath
$Python = Join-Path $Root "py\Scripts\python.exe"
$ProgramDataDir = Join-Path $env:ProgramData "GLKVM Screen Guard"
$ServiceName = "GLKVMGuard"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script as Administrator."
}

sc.exe stop $ServiceName | Out-Null
for ($i = 0; $i -lt 15; $i++) {
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($null -eq $svc -or $svc.Status -eq "Stopped") { break }
    Start-Sleep -Seconds 1
}

Push-Location $Root
try {
    if (Test-Path $Python) {
        & $Python service.py remove 2>$null
    }
} finally {
    Pop-Location
}
sc.exe delete $ServiceName | Out-Null

Remove-Item -LiteralPath (Join-Path $ProgramDataDir "service.json") -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $ProgramDataDir "password.bin") -Force -ErrorAction SilentlyContinue

Get-Service -Name $ServiceName -ErrorAction SilentlyContinue | Format-List Name,Status
Write-Host "Service removed. User data under Documents\\GLKVM Screen Guard is preserved."
