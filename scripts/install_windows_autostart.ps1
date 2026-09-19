param(
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$StartupDir = [Environment]::GetFolderPath("Startup")
$DesktopDir = [Environment]::GetFolderPath("Desktop")
$StartupFile = Join-Path $StartupDir "LuckInit_AutoStart.cmd"
$ShortcutFile = Join-Path $DesktopDir "LuckInit.url"
$MainPy = Join-Path $ProjectDir "main.py"

if ($Remove) {
    if (Test-Path $StartupFile) {
        Remove-Item $StartupFile -Force
    }
    if (Test-Path $ShortcutFile) {
        Remove-Item $ShortcutFile -Force
    }
    Write-Host "LuckInit auto-start removed." -ForegroundColor Yellow
    exit 0
}

if (-not (Test-Path $MainPy)) {
    throw "main.py not found: $MainPy"
}

$PythonCmd = Get-Command python -ErrorAction SilentlyContinue
if ($PythonCmd) {
    $PythonExe = $PythonCmd.Source
} else {
    $KnownPython = Join-Path $env:LOCALAPPDATA "Python\pythoncore-3.14-64\python.exe"
    if (-not (Test-Path $KnownPython)) {
        throw "Python not found. Run: python --version"
    }
    $PythonExe = $KnownPython
}

$PythonDir = Split-Path -Parent $PythonExe
$PythonwExe = Join-Path $PythonDir "pythonw.exe"

if (Test-Path $PythonwExe) {
    $LaunchExe = $PythonwExe
    $StartLine = 'start "" "' + $LaunchExe + '" "' + $MainPy + '" --prod'
} else {
    $LaunchExe = $PythonExe
    $StartLine = 'start "" /min "' + $LaunchExe + '" "' + $MainPy + '" --prod'
}

$CmdLines = @(
    '@echo off',
    'cd /d "' + $ProjectDir + '"',
    $StartLine,
    'exit /b 0'
)
Set-Content -Path $StartupFile -Value $CmdLines -Encoding ASCII

$ShortcutLines = @(
    '[InternetShortcut]',
    'URL=http://127.0.0.1:8000/',
    'IconFile=' + (Join-Path $ProjectDir "favicon.ico"),
    'IconIndex=0'
)
Set-Content -Path $ShortcutFile -Value $ShortcutLines -Encoding ASCII

$AlreadyRunning = $false
try {
    $resp = Invoke-WebRequest "http://127.0.0.1:8000/" -UseBasicParsing -TimeoutSec 2
    if ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 500) {
        $AlreadyRunning = $true
    }
} catch {
}

if (-not $AlreadyRunning) {
    Start-Process -FilePath $LaunchExe -ArgumentList @($MainPy, "--prod") -WorkingDirectory $ProjectDir -WindowStyle Hidden
    Start-Sleep -Seconds 2
}

Write-Host ""
Write-Host "LuckInit auto-start is installed." -ForegroundColor Green
Write-Host "After Windows sign-in, the service will start automatically." -ForegroundColor Green
Write-Host "Desktop shortcut created: LuckInit" -ForegroundColor Cyan
Write-Host "URL: http://127.0.0.1:8000/" -ForegroundColor Cyan
Write-Host ""
Write-Host "To remove auto-start later, run:" -ForegroundColor Yellow
Write-Host ('powershell -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -Remove')
