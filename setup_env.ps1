param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSCommandPath
$Venv = Join-Path $Root "py"
$Python = Join-Path $Venv "Scripts\python.exe"
$Pythonw = Join-Path $Venv "Scripts\pythonw.exe"

if ((Test-Path $Python) -and -not $Force) {
    Write-Host "Python environment already exists: $Venv"
    exit 0
}

if ($Force -and (Test-Path $Venv)) {
    Remove-Item $Venv -Recurse -Force
}

$Candidates = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($ver in @("3.12", "3.11", "3.10")) {
        & py "-$ver" --version *> $null
        if ($LASTEXITCODE -eq 0) {
            $Candidates += ,@("py", "-$ver")
            break
        }
    }
}
if (Get-Command python -ErrorAction SilentlyContinue) {
    $Candidates += ,@("python")
}
if ($Candidates.Count -eq 0) {
    throw "Python 3.10+ not found. Install it from https://www.python.org/downloads/ and enable 'Add python.exe to PATH'."
}

$Candidate = $Candidates[0]
if ($Candidate.Count -eq 2) {
    & $Candidate[0] $Candidate[1] -m venv $Venv
} else {
    & $Candidate[0] -m venv $Venv
}
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $Python)) {
    throw "Failed to create the virtual environment."
}

& $Python -m pip install --disable-pip-version-check --upgrade pip
& $Python -m pip install --disable-pip-version-check PyQt6 pywin32
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install PyQt6/pywin32."
}

Write-Host ""
Write-Host "Done."
Write-Host "  Python : $Python"
Write-Host "  Launch : RunGUI.bat"
Write-Host "  Install: powershell -ExecutionPolicy Bypass -File install.ps1"
