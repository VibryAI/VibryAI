@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

:: ============================================================
:: Vibry AI Core — Windows 部署 & 管理脚本
:: 用法:
::   deploy.bat             首次部署
::   deploy.bat update      更新代码 + 重启
::   deploy.bat start       启动服务
::   deploy.bat stop        停止服务
::   deploy.bat restart     重启服务
::   deploy.bat status      查看状态
::   deploy.bat logs 100    查看最近100行日志
:: ============================================================

set APP_NAME=vibry-server
set APP_DIR=%~dp0
set PORT=9999
set REPO_URL=%VIBRY_REPO%
if "%REPO_URL%"=="" set REPO_URL=https://github.com/VibryAI/VibryAI.git

:: ---- 检查依赖 ----
where python >nul 2>&1 || (echo [ERROR] 请先安装 Python 3.10+ && exit /b 1)
where git >nul 2>&1 || (echo [ERROR] 请先安装 Git && exit /b 1)

:: ---- 命令分发 ----
if "%1"=="update"  goto :update
if "%1"=="start"   goto :start
if "%1"=="stop"    goto :stop
if "%1"=="restart" goto :restart
if "%1"=="status"  goto :status
if "%1"=="logs"    goto :logs
goto :deploy

:: ============================================================
:deploy
echo [Vibry] 首次部署...
if not exist "%APP_DIR%\.git" (
    echo [Vibry] 克隆仓库...
    git clone "%REPO_URL%" "%APP_DIR%"
)
if not exist "%APP_DIR%\venv" (
    echo [Vibry] 创建虚拟环境...
    python -m venv "%APP_DIR%\venv"
)
call "%APP_DIR%\venv\Scripts\activate.bat"
echo [Vibry] 安装依赖...
python -m pip install --upgrade pip -q
python -m pip install -r "%APP_DIR%\requirements.txt" -q
python -m pip install numpy soundfile -q
python -m pip install librosa 2>nul || echo [WARN] librosa 未安装 (声纹 MFCC 将用 FFT fallback)

:: 创建必要目录
mkdir "%APP_DIR%\audio"      2>nul
mkdir "%APP_DIR%\debug"      2>nul
mkdir "%APP_DIR%\voiceprints" 2>nul
mkdir "%APP_DIR%\qdrant_data" 2>nul

:: 检查 .env
if not exist "%APP_DIR%\.env" (
    if exist "%APP_DIR%\.env.example" (
        copy "%APP_DIR%\.env.example" "%APP_DIR%\.env" >nul
        echo [WARN] .env 已从模板创建，请编辑: %APP_DIR%\.env
    )
)

call :start
echo ================================================
echo   部署完成！
echo   管理后台: http://localhost:%PORT%/admin
echo   API 地址: http://localhost:%PORT%/v1
echo ================================================
goto :eof

:: ============================================================
:update
echo [Vibry] 更新代码...
cd /d "%APP_DIR%"
git pull origin master
call "%APP_DIR%\venv\Scripts\activate.bat"
python -m pip install -r "%APP_DIR%\requirements.txt" -q
call :stop
call :start
echo [Vibry] 更新完成！
goto :eof

:: ============================================================
:start
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING" 2^>nul') do (
    echo [WARN] 端口 %PORT% 已被占用 (PID: %%a)
    goto :eof
)
echo [Vibry] 启动服务...
cd /d "%APP_DIR%"
start /b "" "%APP_DIR%\venv\Scripts\python.exe" main.py > server_output.log 2>&1
timeout /t 3 >nul
curl -s http://localhost:%PORT%/api/health >nul 2>&1 && echo [Vibry] ✅ 服务已启动 || echo [ERROR] 启动可能失败，查看 server_output.log
goto :eof

:: ============================================================
:stop
echo [Vibry] 停止服务...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING" 2^>nul') do (
    taskkill /PID %%a /F >nul 2>&1
    echo [Vibry] ✅ 已停止 (PID: %%a)
)
goto :eof

:: ============================================================
:restart
call :stop
timeout /t 2 >nul
call :start
goto :eof

:: ============================================================
:status
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING" 2^>nul') do (
    echo [Vibry] 状态: ✅ 运行中 (PID: %%a, 端口: %PORT%)
    curl -s http://localhost:%PORT%/api/health | python -m json.tool 2>nul
    goto :eof
)
echo [Vibry] 状态: ❌ 未运行
goto :eof

:: ============================================================
:logs
set LINES=%2
if "%LINES%"=="" set LINES=50
if exist "%APP_DIR%\server_output.log" (
    powershell -Command "Get-Content '%APP_DIR%\server_output.log' -Tail %LINES%"
) else (
    echo [WARN] 日志文件不存在
)
goto :eof
