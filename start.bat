@echo off
echo ========================================
echo   MiniMax API 中转平台 启动脚本
echo ========================================
echo.

cd /d "%~dp0"

echo [1/3] 检查 Python 环境...
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.8+
    pause
    exit /b 1
)

echo [2/3] 安装依赖...
pip install -r requirements.txt -q

echo [3/3] 启动服务...
echo.
echo 服务地址: http://localhost:8000
echo 管理后台: http://localhost:8000
echo 首次启动需要在登录页初始化管理员账号
echo.
echo 按 Ctrl+C 停止服务
echo ========================================
echo.

set MINIMAX_API_KEY=your_api_key_here

python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

pause
