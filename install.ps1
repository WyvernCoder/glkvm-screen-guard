param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSCommandPath
$Venv = Join-Path $Root "py"
$Pythonw = Join-Path $Venv "Scripts\pythonw.exe"

if (-not (Test-Path $Pythonw)) {
    Write-Host "Python environment missing; running setup_env.ps1 ..."
    & (Join-Path $Root "setup_env.ps1")
}
if (-not (Test-Path $Pythonw)) {
    throw "Python environment is still missing."
}

$Desktop = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $Desktop "GLKVM Screen Guard.lnk"

$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $Pythonw
$Shortcut.Arguments = '"' + (Join-Path $Root "main.py") + '"'
$Shortcut.WorkingDirectory = $Root
$Icon = Join-Path $Root "assets\icon.ico"
if (Test-Path $Icon) {
    $Shortcut.IconLocation = "$Icon,0"
}
$Shortcut.Description = "GLKVM Screen Guard"
$Shortcut.Save()
Write-Host "Desktop shortcut created: $ShortcutPath"
