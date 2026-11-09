# install_autostart.ps1 -- create/remove a Startup shortcut so the local server
# starts automatically at logon (headless, no console window).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File install_autostart.ps1
#   powershell -ExecutionPolicy Bypass -File install_autostart.ps1 -Remove
#   powershell -ExecutionPolicy Bypass -File install_autostart.ps1 -Dir <target-folder>   # for testing
param(
    [string]$Dir = "",
    [switch]$Remove
)

$root = $PSScriptRoot
if (-not $root) { $root = Split-Path -Parent $MyInvocation.MyCommand.Path }
$startup = if ($Dir) { $Dir } else { Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup' }
$lnkPath = Join-Path $startup 'N-Translate-Server.lnk'

if (-not (Test-Path -LiteralPath $startup)) {
    New-Item -ItemType Directory -Force -Path $startup | Out-Null
}

if ($Remove) {
    if (Test-Path -LiteralPath $lnkPath) {
        Remove-Item -LiteralPath $lnkPath -Force
        Write-Output "[OK] removed shortcut: $lnkPath"
    } else {
        Write-Output "[--] shortcut not found: $lnkPath"
    }
    exit 0
}

$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
$daemon  = Join-Path $root 'server_daemon.py'

if (-not (Test-Path -LiteralPath $pythonw)) {
    Write-Output "[ERR] pythonw.exe not found: $pythonw"
    exit 1
}

$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($lnkPath)
$lnk.TargetPath       = $pythonw
$lnk.Arguments        = '"' + $daemon + '"'
$lnk.WorkingDirectory = $root
$lnk.WindowStyle      = 7
$lnk.Description      = 'Local NPU translation server (headless, starts hidden)'
$lnk.Save()

if (Test-Path -LiteralPath $lnkPath) {
    Write-Output "[OK] autostart enabled: $lnkPath"
    Write-Output "     target: $pythonw `"$daemon`""
    Write-Output "     the server will start silently at every logon (log: $root\server.log)"
} else {
    Write-Output "[ERR] failed to create shortcut"
    exit 1
}
