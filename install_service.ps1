# Installs GLKVM Screen Guard as a Windows service (LocalSystem, auto start).
# Run this in an elevated PowerShell.  It does not create the desktop shortcut.
param(
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSCommandPath
$Python = Join-Path $Root "py\Scripts\python.exe"
$Pythonw = Join-Path $Root "py\Scripts\pythonw.exe"
$Agent = Join-Path $Root "agent.py"
$DataDir = Join-Path ([Environment]::GetFolderPath("MyDocuments")) "GLKVM Screen Guard"
$ProgramDataDir = Join-Path $env:ProgramData "GLKVM Screen Guard"
$ServiceName = "GLKVMGuard"

function Assert-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this script as Administrator."
    }
}

Assert-Admin
if (-not (Test-Path $Python)) { throw "Missing venv: $Python (run setup_env.ps1 first)" }
if (-not (Test-Path $Pythonw)) { throw "Missing pythonw: $Pythonw" }
if (-not (Test-Path $Agent)) { throw "Missing agent: $Agent" }
& $Python -c "import win32service" 2>$null
if ($LASTEXITCODE -ne 0) { throw "pywin32 is missing; run setup_env.ps1 first" }

New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $DataDir "logs") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $DataDir "snapshots") | Out-Null
New-Item -ItemType Directory -Force -Path $ProgramDataDir | Out-Null

$serviceJson = @{
    root      = $Root
    pythonw   = $Pythonw
    agent     = $Agent
    data_dir  = $DataDir
    pipe      = '\\.\pipe\GLKVMGuard-v1'
} | ConvertTo-Json
[IO.File]::WriteAllText(
    (Join-Path $ProgramDataDir "service.json"),
    $serviceJson,
    (New-Object System.Text.UTF8Encoding($false))
)

# Stop and remove any previous installation.
sc.exe stop $ServiceName | Out-Null
for ($i = 0; $i -lt 15; $i++) {
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($null -eq $svc -or $svc.Status -eq "Stopped") { break }
    Start-Sleep -Seconds 1
}
Push-Location $Root
try {
    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        & $Python service.py remove 2>$null
    }
} finally {
    Pop-Location
}
Start-Sleep -Seconds 1

Push-Location $Root
try {
    & $Python service.py --startup auto install
    if ($LASTEXITCODE -ne 0) { throw "service install failed" }
} finally {
    Pop-Location
}

# pywin32's pythonservice.exe runs with the base Python.  Give the service an
# explicit PYTHONPATH (venv packages + project root) and PATH.
$site = Join-Path $Root "py\Lib\site-packages"
$pyvenv = Get-Content (Join-Path $Root "py\pyvenv.cfg") -ErrorAction SilentlyContinue
$homeLine = $pyvenv | Where-Object { $_ -match '^\s*home\s*=' } | Select-Object -First 1
$baseHome = ""
if ($homeLine) { $baseHome = ($homeLine -split "=", 2)[1].Trim() }
$pythonPath = @(
    $Root,
    $site,
    (Join-Path $site "win32"),
    (Join-Path $site "win32\lib"),
    (Join-Path $site "pythonwin")
) -join ";"
$pathValue = @()
if ($baseHome) { $pathValue += $baseHome }
$pathValue += (Join-Path $Root "py")
$pathValue += $env:PATH
$pathValue = $pathValue -join ";"
$svcKey = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName"
New-ItemProperty -Path $svcKey -Name Environment -PropertyType MultiString -Value @("PYTHONPATH=$pythonPath", "PATH=$pathValue") -Force | Out-Null

# Sync the machine-scope KVM password from the current user's config, if any.
$configPath = Join-Path $DataDir "config.ini"
if (Test-Path $configPath) {
    Push-Location $Root
    try {
        & $Python -c "from app.config import Config; from app.paths import CONFIG_PATH; from app.service_paths import save_password_machine; save_password_machine(Config(CONFIG_PATH).password())"
    } catch {
        Write-Warning "Could not sync the KVM password: $($_.Exception.Message)"
    } finally {
        Pop-Location
    }
}

if (-not $NoStart) {
    Start-Service -Name $ServiceName
    Start-Sleep -Seconds 4
}
Get-Service -Name $ServiceName | Format-List Name,Status,StartType
Write-Host ""
Write-Host "Service data: $ProgramDataDir"
Write-Host "User data   : $DataDir"
Write-Host "Open the GUI; it will connect to the service automatically."
