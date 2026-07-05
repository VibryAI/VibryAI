@echo off
chcp 65001 >nul
title Vibry AI Core — 记忆代理 + AI分析 (:9999)

cd /d "%~dp0"

echo.
echo ═══════════════════════════════════════════════════════
echo   🧠 Vibry AI Core — 数字前额叶 + AI 分析后端
echo ═══════════════════════════════════════════════════════
echo.

REM 检查 .env
if not exist ".env" (
    echo ⚠️  未找到 .env，从 .env.example 复制...
    copy .env.example .env
    echo 📝 请编辑 .env 填入配置
    echo.
)

REM 检查 Python
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo ❌ 未找到 Python，请先安装 Python 3.10+
    pause
    exit /b 1
)

echo 🐍 Python:
python --version

REM 检查/创建虚拟环境
if not exist "venv\" (
    echo 📦 创建虚拟环境...
    python -m venv venv
    echo 📥 安装依赖...
    venv\Scripts\python -m pip install -r requirements.txt -q
)

echo.
echo 🚀 启动 Vibry AI Core...
echo    端口: 9999
echo.
echo    📡 API 端点:
echo       OpenAI 代理:  http://localhost:9999/v1/chat/completions
echo       语音转文字:   http://localhost:9999/api/transcribe
echo       会议纪要:     http://localhost:9999/api/summarize
echo       录音管理:     http://localhost:9999/api/recordings
echo       记忆管理:     http://localhost:9999/api/memories
echo       健康检查:     http://localhost:9999/api/health
echo.
echo    🔧 客户端配置:
echo       Cursor/LobeChat Base URL = http://localhost:9999/v1
echo       API Key = 你的用户ID (Bearer token → user_id)
echo.
echo ═══════════════════════════════════════════════════════
echo.

venv\Scripts\python main.py

pause
