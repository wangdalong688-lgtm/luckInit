param(
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $PSScriptRoot
$StartupDir = [Environment]::GetFolderPath("Startup")
$DesktopDir = [Environment]::GetFolderPath("Desktop")
$StartupFile = Join-Path $StartupDir "LuckInit_AutoStart.cmd"
$ShortcutFile = Join-Path $DesktopDir "瑞幸选址工具.url"
$MainPy = Join-Path $ProjectDir "main.py"

if ($Remove) {
    if (Test-Path $StartupFile) {
        Remove-Item $StartupFile -Force
    }
    if (Test-Path $ShortcutFile) {
        Remove-Item $ShortcutFile -Force
    }
    Write-Host "已取消 LuckInit 开机自动启动。" -ForegroundColor Yellow
    exit 0
}

if (-not (Test-Path $MainPy)) {
    throw "未找到 main.py：$MainPy"
}

$PythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $PythonCmd) {
    $KnownPython = Join-Path $env:LOCALAPPDATA "Python\pythoncore-3.14-64\python.exe"
    if (Test-Path $KnownPython) {
        $PythonExe = $KnownPython
    } else {
        throw "未找到 Python。请先确认在 PowerShell 中可以运行 python --version。"
    }
} else {
    $PythonExe = $PythonCmd.Source
}

$PythonDir = Split-Path -Parent $PythonExe
$PythonwExe = Join-Path $PythonDir "pythonw.exe"
$UsePythonw = Test-Path $PythonwExe

if ($UsePythonw) {
    $LaunchExe = $PythonwExe
    $StartLine = 'start "" "' + $LaunchExe + '" "' + $MainPy + '" --prod'
} else {
    $LaunchExe = $PythonExe
    $StartLine = 'start "" /min "' + $LaunchExe + '" "' + $MainPy + '" --prod'
}

$CmdContent = @"
@echo off
cd /d "$ProjectDir"
$StartLine
exit /b 0
"@

Set-Content -Path $StartupFile -Value $CmdContent -Encoding ASCII

$ShortcutContent = @"
[InternetShortcut]
URL=http://127.0.0.1:8000/
IconFile=$ProjectDir\favicon.ico
IconIndex=0
"@
Set-Content -Path $ShortcutFile -Value $ShortcutContent -Encoding Unicode

$AlreadyRunning = $false
try {
    $resp = Invoke-WebRequest "http://127.0.0.1:8000/" -UseBasicParsing -TimeoutSec 2
    if ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 500) {
        $AlreadyRunning = $true
    }
} catch {}

if (-not $AlreadyRunning) {
    Start-Process -FilePath $LaunchExe -ArgumentList @($MainPy, "--prod") -WorkingDirectory $ProjectDir -WindowStyle Hidden
    Start-Sleep -Seconds 2
}

Write-Host ""
Write-Host "LuckInit 开机自动启动已配置完成。" -ForegroundColor Green
Write-Host "电脑登录 Windows 后会自动启动服务，不需要再开 PowerShell。" -ForegroundColor Green
Write-Host "桌面已创建：瑞幸选址工具" -ForegroundColor Cyan
Write-Host "本机网址：http://127.0.0.1:8000/" -ForegroundColor Cyan
Write-Host ""
Write-Host "如需取消自动启动，请执行：" -ForegroundColor Yellow
Write-Host "powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Remove"
