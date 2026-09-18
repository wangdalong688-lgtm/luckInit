@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ========================================
echo LuckInit 瑞幸选址工具 - 本地启动
echo ========================================

set "PY=python"
python --version >nul 2>&1
if errorlevel 1 (
  py --version >nul 2>&1
  if errorlevel 1 (
    echo [ERROR] 未找到 Python。
    pause
    exit /b 1
  )
  set "PY=py"
)

echo [1/4] 检查依赖...
%PY% -c "import fastapi,uvicorn,requests" >nul 2>&1
if errorlevel 1 (
  echo 正在通过清华镜像安装依赖...
  %PY% -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  if errorlevel 1 (
    echo [ERROR] 依赖安装失败。
    pause
    exit /b 1
  )
)

echo [2/4] 检查 8000 端口...
netstat -ano | findstr /R /C:":8000 .*LISTENING" >nul 2>&1
if errorlevel 1 (
  echo 正在启动服务...
  start "LuckInit Server" cmd /k "cd /d ""%~dp0"" && %PY% main.py"
) else (
  echo 服务已在运行。
)

echo [3/4] 等待服务就绪...
powershell -NoProfile -Command "$ok=$false; for($i=0;$i -lt 15;$i++){ try { $r=Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8000/' -TimeoutSec 1; if($r.StatusCode -ge 200){$ok=$true; break} } catch {}; Start-Sleep -Seconds 1 }; if(-not $ok){ exit 1 }"
if errorlevel 1 (
  echo [ERROR] 服务未能启动，请保留 LuckInit Server 窗口并截图发给 ChatGPT。
  pause
  exit /b 1
)

echo [4/4] 打开登录页...
start "" "http://127.0.0.1:8000/"
exit /b 0
