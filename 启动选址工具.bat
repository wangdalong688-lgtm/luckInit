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
    echo [ERROR] 未找到 Python，请先安装 Python。
    pause
    exit /b 1
  )
  set "PY=py"
)

echo [1/3] 检查依赖...
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

echo [2/3] 启动浏览器...
start "" cmd /c "timeout /t 4 /nobreak >nul & start http://127.0.0.1:8000/city_analysis"

echo [3/3] 启动服务...
echo 关闭服务请在本窗口按 Ctrl+C。
%PY% main.py

pause
