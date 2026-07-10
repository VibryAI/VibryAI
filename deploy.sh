#!/bin/bash
set -e

# ============================================================
# Vibry AI Core — 一键部署脚本
# 用法:
#   bash deploy.sh              # 首次部署
#   bash deploy.sh --update     # 更新已有部署
#   bash deploy.sh --start      # 仅启动
#   bash deploy.sh --stop       # 停止服务
#   bash deploy.sh --status     # 查看状态
# ============================================================

APP_NAME="vibry-server"
APP_DIR="/opt/vibry-server"
VENV_DIR="$APP_DIR/venv"
PORT=9999
REPO_URL="${VIBRY_REPO:-https://github.com/VibryAI/VibryAI.git}"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}[Vibry]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()  { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# ---- 检查依赖 ----
check_deps() {
    command -v python3 >/dev/null 2>&1 || err "请先安装 Python 3.10+"
    command -v git >/dev/null 2>&1 || err "请先安装 Git"
    command -v ffmpeg >/dev/null 2>&1 || warn "ffmpeg 未安装，转写功能需要它"
    log "依赖检查通过"
}

# ---- 创建目录 ----
setup_dirs() {
    mkdir -p "$APP_DIR"
    mkdir -p "$APP_DIR/audio"
    mkdir -p "$APP_DIR/debug"
    mkdir -p "$APP_DIR/voiceprints"
    mkdir -p "$APP_DIR/qdrant_data"
    mkdir -p "$APP_DIR/raw"
    mkdir -p "$APP_DIR/wiki"
    log "目录结构已创建"
}

# ---- 克隆/更新代码 ----
deploy_code() {
    if [ -d "$APP_DIR/.git" ]; then
        log "更新已有代码..."
        cd "$APP_DIR"
        git pull origin master
    else
        log "克隆仓库..."
        git clone "$REPO_URL" "$APP_DIR"
    fi
}

# ---- 虚拟环境 ----
setup_venv() {
    if [ ! -d "$VENV_DIR" ]; then
        log "创建 Python 虚拟环境..."
        python3 -m venv "$VENV_DIR"
    fi
    source "$VENV_DIR/bin/activate"
    log "安装 Python 依赖..."
    pip install --upgrade pip -q
    pip install -r "$APP_DIR/requirements.txt" -q
    # 声纹识别额外依赖
    pip install numpy soundfile -q
    pip install librosa 2>/dev/null || warn "librosa 未安装 (声纹 MFCC 将用 FFT fallback)"
    log "依赖安装完成"
}

# ---- 配置 .env ----
setup_env() {
    if [ ! -f "$APP_DIR/.env" ]; then
        if [ -f "$APP_DIR/.env.example" ]; then
            cp "$APP_DIR/.env.example" "$APP_DIR/.env"
            warn ".env 已从模板创建，请编辑配置: $APP_DIR/.env"
            echo ""
            echo "  必填项:"
            echo "    UPSTREAM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3"
            echo "    UPSTREAM_API_KEY=your-doubao-api-key"
            echo "    DOUBAO_ASR_APP_ID=your-app-id"
            echo "    DOUBAO_ASR_ACCESS_KEY=your-access-key"
            echo "    ADMIN_PASSWORD=your-admin-password"
            echo ""
            echo "  Wiki 编译 (可选):"
            echo "    WIKI_MODEL=deepseek-chat"
            echo "    WIKI_BASE_URL=https://api.deepseek.com"
            echo "    WIKI_API_KEY=your-deepseek-api-key"
        else
            err "缺少 .env 文件"
        fi
    else
        log ".env 已存在，跳过"
    fi
}

# ---- 启动服务 ----
start_server() {
    local pid=$(cat "$APP_DIR/.pid" 2>/dev/null || echo "")
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        warn "服务已在运行 (PID: $pid)"
        return
    fi

    cd "$APP_DIR"
    source "$VENV_DIR/bin/activate"

    log "启动 Vibry AI Core 服务..."
    nohup python main.py > server_output.log 2>&1 &
    echo $! > "$APP_DIR/.pid"
    sleep 2

    pid=$(cat "$APP_DIR/.pid")
    if kill -0 "$pid" 2>/dev/null; then
        log "✅ 服务已启动"
        log "   PID: $pid"
        log "   端口: $PORT"
        log "   日志: $APP_DIR/server_output.log"
        log "   管理: http://localhost:$PORT/admin"
    else
        err "启动失败，查看日志: $APP_DIR/server_output.log"
    fi
}

# ---- 停止服务 ----
stop_server() {
    local pid=$(cat "$APP_DIR/.pid" 2>/dev/null || echo "")
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null
        sleep 1
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
        rm -f "$APP_DIR/.pid"
        log "✅ 服务已停止 (PID: $pid)"
    else
        warn "服务未在运行"
    fi
}

# ---- 查看状态 ----
show_status() {
    local pid=$(cat "$APP_DIR/.pid" 2>/dev/null || echo "")
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        log "服务状态: ✅ 运行中"
        echo "  PID: $pid"
        echo "  端口: $PORT"
        echo "  日志: $(du -h "$APP_DIR/server_output.log" 2>/dev/null | cut -f1)"

        if command -v curl >/dev/null 2>&1; then
            echo ""
            curl -s "http://localhost:$PORT/api/health" | python3 -m json.tool 2>/dev/null || true
        fi
    else
        warn "服务状态: ❌ 未运行"
    fi
}

# ---- 查看日志 ----
show_logs() {
    local lines=${1:-50}
    if [ -f "$APP_DIR/server_output.log" ]; then
        tail -n "$lines" "$APP_DIR/server_output.log"
    else
        warn "日志文件不存在"
    fi
}

# ============================================================
# 主入口
# ============================================================

case "${1:-}" in
    --update)
        check_deps
        setup_dirs
        deploy_code
        setup_venv
        setup_env
        stop_server
        start_server
        ;;
    --start)
        start_server
        ;;
    --stop)
        stop_server
        ;;
    --restart)
        stop_server
        sleep 1
        start_server
        ;;
    --status)
        show_status
        ;;
    --logs)
        show_logs "${2:-50}"
        ;;
    --help|-h)
        echo "用法: bash deploy.sh [选项]"
        echo ""
        echo "  无参数       完整首次部署"
        echo "  --update     更新代码 + 重启"
        echo "  --start      启动服务"
        echo "  --stop       停止服务"
        echo "  --restart    重启服务"
        echo "  --status     查看状态"
        echo "  --logs [N]   查看最近 N 行日志 (默认50)"
        echo ""
        echo "环境变量:"
        echo "  VIBRY_REPO   仓库地址 (默认 GitHub)"
        ;;
    *)
        check_deps
        setup_dirs
        deploy_code
        setup_venv
        setup_env
        start_server
        log ""
        log "================================================"
        log "  部署完成！"
        log "  管理后台: http://<服务器IP>:$PORT/admin"
        log "  API 地址: http://<服务器IP>:$PORT/v1"
        log "  首次登录请用管理员密码"
        log "================================================"
        ;;
esac
