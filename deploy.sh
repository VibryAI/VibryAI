#!/bin/bash
# ============================================================
# Vibry AI Core — Linux 一键部署 & 管理脚本
# ============================================================
# 用法:
#   sudo bash deploy.sh              # 首次部署 (从 tar.gz 解压后运行)
#   sudo bash deploy.sh --update     # 更新 + 重启 (git pull)
#   sudo bash deploy.sh --start      # 启动服务
#   sudo bash deploy.sh --stop       # 停止服务
#   sudo bash deploy.sh --restart    # 重启服务
#   sudo bash deploy.sh --status     # 查看状态
#   sudo bash deploy.sh --logs [N]   # 查看日志
#   sudo bash deploy.sh --uninstall  # 完全卸载
#   sudo bash deploy.sh --nginx      # 生成 Nginx 反向代理配置
# ============================================================

set -e

# ---- 配置 (支持环境变量覆盖) ----
APP_DIR="${VIBRY_HOME:-/opt/http/vibryai/server}"
APP_NAME="${VIBRY_SERVICE:-vibry-server}"
VENV_DIR="${APP_DIR}/venv"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
NGINX_CONF="/etc/nginx/sites-available/${APP_NAME}"
ENV_FILE="${APP_DIR}/.env"
PID_FILE="${APP_DIR}/.pid"
LOG_FILE="${APP_DIR}/data/logs/server.log"
BACKUP_DIR="${APP_DIR}/data/backups/deploy"
PORT="${VIBRY_PORT:-9999}"
DOMAIN="${VIBRY_DOMAIN:-163.7.8.8}"
USER="${VIBRY_USER:-vibry}"
GROUP="${VIBRY_GROUP:-vibry}"

# ---- 颜色 ----
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'
log()  { echo -e "${GREEN}[Vibry]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()  { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
info() { echo -e "${CYAN}       $1${NC}"; }

must_be_root() {
    if [ "$(id -u)" -ne 0 ]; then
        err "请用 sudo 运行此脚本"
    fi
}

# ============================================================
# 1. 系统依赖安装
# ============================================================
install_system_deps() {
    log "检查 & 安装系统依赖..."

    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -qq
        apt-get install -y -qq python3 python3-venv python3-pip ffmpeg curl git nginx rsync lsof 2>&1 | tail -1
    elif command -v yum >/dev/null 2>&1; then
        yum install -y python3 python3-pip ffmpeg curl git nginx rsync lsof 2>&1 | tail -1
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y python3 python3-pip ffmpeg curl git nginx rsync lsof 2>&1 | tail -1
    elif command -v apk >/dev/null 2>&1; then
        apk add --no-cache python3 py3-pip py3-venv ffmpeg curl git nginx rsync lsof
    else
        warn "未识别包管理器，请手动安装: python3.10+, ffmpeg, nginx"
    fi

    # 验证关键依赖
    command -v python3 >/dev/null 2>&1 || err "python3 未安装"
    log "系统依赖 OK"
}

# ============================================================
# 2. 创建专用用户 & 目录
# ============================================================
setup_user_and_dirs() {
    log "创建运行用户 & 目录..."

    # 创建用户和运行组
    if ! getent group "$GROUP" >/dev/null 2>&1; then
        groupadd --system "$GROUP" 2>/dev/null || true
    fi
    if ! id -u "$USER" >/dev/null 2>&1; then
        useradd --system --no-create-home --gid "$GROUP" --shell /usr/sbin/nologin "$USER" 2>/dev/null || \
        useradd --system --shell /sbin/nologin "$USER" 2>/dev/null || true
        log "用户 $USER 已创建"
    fi

    # 创建目录
    mkdir -p "$APP_DIR"
    mkdir -p "$APP_DIR/data/audio"
    mkdir -p "$APP_DIR/data/debug"
    mkdir -p "$APP_DIR/data/voiceprints"
    mkdir -p "$APP_DIR/data/logs"

    log "目录结构已创建"
}

# ============================================================
# 3. 运行数据备份与失败回滚
# ============================================================
CURRENT_BACKUP_DIR=""

backup_runtime() {
    if [ ! -f "$APP_DIR/run.py" ]; then
        return 0
    fi

    local stamp
    stamp="$(date +%Y%m%d-%H%M%S)"
    CURRENT_BACKUP_DIR="${BACKUP_DIR}/${stamp}"
    mkdir -p "$CURRENT_BACKUP_DIR"

    log "备份当前版本到 $CURRENT_BACKUP_DIR ..."
    tar -C "$APP_DIR" -czf "$CURRENT_BACKUP_DIR/code.tar.gz" \
        --exclude='./venv' \
        --exclude='./data' \
        --exclude='./.env' \
        --exclude='./.cache' \
        --exclude='./release' \
        --exclude='./__pycache__' \
        --exclude='*.pyc' \
        .

    cp "$ENV_FILE" "$CURRENT_BACKUP_DIR/.env" 2>/dev/null || true
    if [ -f "$APP_DIR/data/vibrycard.db" ]; then
        cp "$APP_DIR/data/vibrycard.db" "$CURRENT_BACKUP_DIR/vibrycard.db"
    fi
    log "数据库、配置和旧代码已备份"
}

rollback_runtime() {
    if [ -z "$CURRENT_BACKUP_DIR" ] || [ ! -f "$CURRENT_BACKUP_DIR/code.tar.gz" ]; then
        warn "没有可用的部署前备份，无法自动回滚"
        return 1
    fi

    warn "部署检查失败，正在回滚到更新前版本..."
    stop_server || true
    local restore_dir
    restore_dir="$(mktemp -d)"
    tar -xzf "$CURRENT_BACKUP_DIR/code.tar.gz" -C "$restore_dir"
    rsync -a --delete \
        --exclude='venv' \
        --exclude='data' \
        --exclude='.env' \
        --exclude='.cache' \
        "$restore_dir"/ "$APP_DIR"/
    rm -rf "$restore_dir"

    if [ -f "$CURRENT_BACKUP_DIR/.env" ]; then
        cp "$CURRENT_BACKUP_DIR/.env" "$ENV_FILE"
    fi
    if [ -f "$CURRENT_BACKUP_DIR/vibrycard.db" ]; then
        rm -f "$APP_DIR/data/vibrycard.db-wal" "$APP_DIR/data/vibrycard.db-shm"
        cp "$CURRENT_BACKUP_DIR/vibrycard.db" "$APP_DIR/data/vibrycard.db"
    fi

    setup_systemd
    fix_permissions
    start_server
    log "已回滚到更新前版本"
}

# ============================================================
# 4. 部署代码 (从压缩包或 git)
# ============================================================
deploy_code() {
    local SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

    # 判断：当前脚本目录就是 APP_DIR（已部署），还是需要复制
    if [ "$SRC_DIR" = "$APP_DIR" ]; then
        log "代码已在 $APP_DIR，跳过复制"
        return
    fi

    # 检查是否是 git 仓库
    if [ -d "$SRC_DIR/.git" ] && [ "$SRC_DIR" != "$APP_DIR" ]; then
        log "从 Git 仓库部署..."
        if [ -d "$APP_DIR/.git" ]; then
            cd "$APP_DIR"
            git pull origin "$(git branch --show-current)" 2>/dev/null || git pull origin main
        else
            cp -r "$SRC_DIR" "$APP_DIR"
        fi
        return
    fi

    # 从解压目录复制
    if [ "$SRC_DIR" != "$APP_DIR" ]; then
        log "从 $SRC_DIR 复制代码到 $APP_DIR ..."
        rsync -a --delete \
            --exclude='venv' \
            --exclude='data' \
            --exclude='__pycache__' \
            --exclude='*.pyc' \
            --exclude='*.db' \
            --exclude='*.db-shm' \
            --exclude='*.db-wal' \
            --exclude='.env' \
            --exclude='*.log' \
            --exclude='.pid' \
            --exclude='release' \
            --exclude='ffmpeg-win-*' \
            --exclude='nssm.*' \
            --exclude='*.bat' \
            --exclude='service.py' \
            --exclude='*.zip' \
            "$SRC_DIR"/ "$APP_DIR"/
    fi

    log "代码部署完成"
}

# ============================================================
# 4. Python 虚拟环境 & 依赖
# ============================================================
setup_venv() {
    log "设置 Python 虚拟环境..."

    if [ ! -d "$VENV_DIR" ]; then
        python3 -m venv "$VENV_DIR"
    fi

    source "$VENV_DIR/bin/activate"
    pip install --upgrade pip -q

    log "安装 Python 依赖 (可能需要几分钟)..."
    pip install -r "$APP_DIR/requirements.txt" -q

    # 可选依赖
    pip install numpy soundfile -q
    pip install librosa 2>/dev/null && log "librosa OK" || warn "librosa 未安装 (声纹将用 FFT fallback)"

    log "Python 依赖安装完成"
}

# ============================================================
# 5. 配置 .env
# ============================================================
setup_env() {
    log "配置 .env..."

    if [ ! -f "$ENV_FILE" ]; then
        if [ -f "$APP_DIR/.env.example" ]; then
            cp "$APP_DIR/.env.example" "$ENV_FILE"
        fi
    fi

    # 强制设置 Linux 关键项 (默认 127.0.0.1，Nginx 反代到本地)
    sed -i "s/^SERVER_HOST=.*/SERVER_HOST=127.0.0.1/" "$ENV_FILE" 2>/dev/null || true
    sed -i "s/^SERVER_PORT=.*/SERVER_PORT=${PORT}/" "$ENV_FILE" 2>/dev/null || true
    sed -i "s/^FFMPEG_PATH=.*/FFMPEG_PATH=ffmpeg/" "$ENV_FILE" 2>/dev/null || true
    sed -i "s/^LOG_LEVEL=.*/LOG_LEVEL=INFO/" "$ENV_FILE" 2>/dev/null || true

    # 追加缺失的关键项
    grep -q "^SERVER_HOST=" "$ENV_FILE" || echo "SERVER_HOST=127.0.0.1" >> "$ENV_FILE"
    grep -q "^SERVER_PORT=" "$ENV_FILE" || echo "SERVER_PORT=${PORT}" >> "$ENV_FILE"
    grep -q "^FFMPEG_PATH=" "$ENV_FILE" || echo "FFMPEG_PATH=ffmpeg" >> "$ENV_FILE"

    chmod 600 "$ENV_FILE"

    warn "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    warn "  请编辑 .env 填入 API 密钥:"
    warn "    nano $ENV_FILE"
    warn ""
    warn "  必填项:"
    warn "    UPSTREAM_API_KEY     — 豆包/DeepSeek API Key"
    warn "    DOUBAO_ASR_APP_ID    — 豆包 ASR App ID"
    warn "    DOUBAO_ASR_ACCESS_KEY— 豆包 ASR Access Key"
    warn "    ADMIN_PASSWORD       — 管理后台密码"
    warn "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

# ============================================================
# 6. systemd 服务
# ============================================================
setup_systemd() {
    log "配置 systemd 服务..."

    cat > "$SERVICE_FILE" << SYSTEMD_EOF
[Unit]
Description=Vibry AI Core — Digital Prefrontal Cortex Memory Proxy + AI Backend
After=network.target network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${USER}
Group=${GROUP}
WorkingDirectory=${APP_DIR}
Environment=PATH=${VENV_DIR}/bin:/usr/local/bin:/usr/bin:/bin
Environment=HF_HOME=${APP_DIR}/.cache/huggingface
ExecStart=${VENV_DIR}/bin/python ${APP_DIR}/run.py
ExecReload=/bin/kill -HUP \$MAINPID
Restart=always
RestartSec=5
# 应用自身写入 data/logs/server.log；systemd 只保留 journal，避免日志重复。
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SYSTEMD_EOF

    # 创建缓存目录
    mkdir -p "${APP_DIR}/.cache/huggingface"
    chown -R "${USER}:${GROUP}" "$APP_DIR" 2>/dev/null || true

    systemctl daemon-reload
    systemctl enable "$APP_NAME" 2>/dev/null || true
    log "systemd 服务已配置: $SERVICE_FILE"
}

# ============================================================
# 7. Nginx 反向代理
# ============================================================
setup_nginx() {
    local NGINX_DOMAIN="${1:-${DOMAIN}}"
    if [ -z "$NGINX_DOMAIN" ]; then
        if command -v curl >/dev/null 2>&1; then
            NGINX_DOMAIN=$(curl -s ifconfig.me 2>/dev/null || echo "your-domain.com")
        else
            NGINX_DOMAIN="your-domain.com"
        fi
    fi

    log "生成 Nginx 配置 (域名: $NGINX_DOMAIN)..."

    cat > "${NGINX_CONF}.conf" << NGINX_EOF
# Vibry AI Core — Nginx Reverse Proxy
# 域名: ${NGINX_DOMAIN}
# 后端: http://127.0.0.1:${PORT}

server {
    listen 80;
    server_name ${NGINX_DOMAIN};

    # 上传限制 (音频文件)
    client_max_body_size 500M;

    # 日志
    access_log /var/log/nginx/vibry-access.log;
    error_log  /var/log/nginx/vibry-error.log;

    location / {
        proxy_pass http://127.0.0.1:${PORT};
        proxy_http_version 1.1;

        # WebSocket / SSE 支持
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        # 流式响应 (SSE 纪要生成可能较慢)
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }

    # 静态文件缓存 (^~ 确保优先级高于正则 location, 避免 .js/.css 被截走 404)
    location ^~ /static/ {
        proxy_pass http://127.0.0.1:${PORT}/static/;
        expires 7d;
        add_header Cache-Control "public, immutable";
    }
}
NGINX_EOF

    # 启用站点
    if [ -d /etc/nginx/sites-enabled ]; then
        ln -sf "${NGINX_CONF}.conf" "/etc/nginx/sites-enabled/${APP_NAME}.conf"
        log "Nginx 站点已启用"
    fi

    # 测试并重载
    if nginx -t 2>/dev/null; then
        systemctl reload nginx 2>/dev/null || true
        log "Nginx 配置已生效: ${NGINX_CONF}.conf"
        echo ""
        info "Nginx 已就绪，接下来配置 HTTPS:"
        info "  sudo apt install certbot python3-certbot-nginx -y"
        info "  sudo certbot --nginx -d ${NGINX_DOMAIN}"
    else
        warn "Nginx 配置测试未通过，请检查: ${NGINX_CONF}.conf"
    fi
}

# ============================================================
# 8. 防火墙
# ============================================================
setup_firewall() {
    log "配置防火墙..."

    if command -v ufw >/dev/null 2>&1; then
        ufw allow ${PORT}/tcp comment "Vibry AI Core" 2>/dev/null || true
        ufw allow 80/tcp comment "Vibry Nginx HTTP" 2>/dev/null || true
        ufw allow 443/tcp comment "Vibry Nginx HTTPS" 2>/dev/null || true
        log "UFW 规则已添加"
    elif command -v firewall-cmd >/dev/null 2>&1; then
        firewall-cmd --permanent --add-port=${PORT}/tcp 2>/dev/null || true
        firewall-cmd --permanent --add-service=http 2>/dev/null || true
        firewall-cmd --permanent --add-service=https 2>/dev/null || true
        firewall-cmd --reload 2>/dev/null || true
        log "firewalld 规则已添加"
    else
        warn "未检测到防火墙，请手动开放端口 ${PORT}"
    fi
}

# ============================================================
# 9. 权限修复
# ============================================================
fix_permissions() {
    log "修复文件权限..."
    chown -R "${USER}:${GROUP}" "$APP_DIR" 2>/dev/null || true
    chmod 600 "$ENV_FILE" 2>/dev/null || true
    chmod +x "$APP_DIR"/deploy.sh 2>/dev/null || true
    log "权限已修复"
}

# ============================================================
# 10. 启动服务
# ============================================================
start_server() {
    if systemctl is-active --quiet "$APP_NAME" 2>/dev/null; then
        warn "服务已在运行 (systemd)"
        return
    fi

    if [ -f "$PID_FILE" ]; then
        local pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            warn "服务已在运行 (PID: $pid)"
            return
        fi
        rm -f "$PID_FILE"
    fi

    # 优先用 systemd
    if [ -f "$SERVICE_FILE" ]; then
        systemctl start "$APP_NAME"
        sleep 2
        if systemctl is-active --quiet "$APP_NAME"; then
            log "✅ 服务已启动 (systemd)"
            systemctl status "$APP_NAME" --no-pager -l 2>/dev/null | head -5
            return
        else
            err "systemd 启动失败，查看: journalctl -u $APP_NAME -n 50"
        fi
    fi

    # nohup 备用
    log "使用 nohup 模式启动..."
    cd "$APP_DIR"
    source "$VENV_DIR/bin/activate"
    nohup python run.py > "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 2
    if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        log "✅ 服务已启动 (nohup, PID: $(cat "$PID_FILE"))"
    else
        err "启动失败，查看日志: $LOG_FILE"
    fi
}

wait_for_health() {
    local attempts="${1:-45}"
    local body=""
    log "等待健康检查: http://127.0.0.1:${PORT}/api/health"
    for ((i=1; i<=attempts; i++)); do
        body="$(curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/api/health" 2>/dev/null || true)"
        if [ -n "$body" ]; then
            log "✅ 健康检查通过"
            echo "$body"
            return 0
        fi
        sleep 2
    done
    warn "健康检查超时"
    return 1
}

update_release() {
    must_be_root
    local src_dir
    src_dir="$(cd "$(dirname "$0")" && pwd)"
    if [ "$src_dir" = "$APP_DIR" ]; then
        err "--update 必须从新版本解压目录运行，不能在当前安装目录内原地更新"
    fi

    setup_user_and_dirs
    stop_server
    if ! backup_runtime; then
        start_server || true
        err "部署前备份失败，已取消更新"
    fi

    if ! deploy_code || ! setup_venv || ! setup_env || ! setup_systemd || ! fix_permissions; then
        rollback_runtime || true
        err "全量更新失败，已尝试回滚"
    fi

    if ! start_server || ! wait_for_health; then
        rollback_runtime || true
        err "新版本未通过健康检查，已回滚"
    fi

    setup_nginx "$DOMAIN"

    log "全量更新完成，部署前备份: $CURRENT_BACKUP_DIR"
}

# ============================================================
# 11. 停止服务
# ============================================================
stop_server() {
    if systemctl is-active --quiet "$APP_NAME" 2>/dev/null; then
        systemctl stop "$APP_NAME"
        log "✅ 服务已停止 (systemd)"
        return
    fi

    if [ -f "$PID_FILE" ]; then
        local pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null
            sleep 2
            kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
            rm -f "$PID_FILE"
            log "✅ 服务已停止 (PID: $pid)"
            return
        fi
        rm -f "$PID_FILE"
    fi

    # 兜底：按端口杀
    local pids=$(lsof -ti :${PORT} 2>/dev/null || true)
    if [ -n "$pids" ]; then
        kill $pids 2>/dev/null
        log "✅ 已停止端口 ${PORT} 上的进程"
    else
        warn "服务未在运行"
    fi
}

# ============================================================
# 12. 查看状态
# ============================================================
show_status() {
    echo ""
    echo -e "${GREEN}════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  Vibry AI Core — 服务状态${NC}"
    echo -e "${GREEN}════════════════════════════════════════════════${NC}"

    local running=false

    if systemctl is-active --quiet "$APP_NAME" 2>/dev/null; then
        echo -e "  状态: ${GREEN}✅ 运行中 (systemd)${NC}"
        systemctl status "$APP_NAME" --no-pager -l 2>/dev/null | grep -E "Active:|Main PID:|Memory:" | sed 's/^/  /'
        running=true
    elif [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo -e "  状态: ${GREEN}✅ 运行中 (nohup, PID: $(cat "$PID_FILE"))${NC}"
        running=true
    else
        echo -e "  状态: ${RED}❌ 未运行${NC}"
    fi

    if [ -f "$LOG_FILE" ]; then
        echo -e "  日志: $(du -h "$LOG_FILE" 2>/dev/null | cut -f1) (${LOG_FILE})"
    fi

    # 版本信息
    if [ -f "$APP_DIR/app/main.py" ]; then
        local ver=$(grep 'version=' "$APP_DIR/app/main.py" 2>/dev/null | head -1 | grep -oP '"[0-9.]+"' | tr -d '"')
        [ -n "$ver" ] && echo -e "  版本: ${ver}"
    fi

    if $running && command -v curl >/dev/null 2>&1; then
        echo ""
        echo -e "  ${CYAN}─ 健康检查 ──────────────────────────────${NC}"
        curl -s --max-time 5 "http://127.0.0.1:${PORT}/api/health" | python3 -m json.tool 2>/dev/null || echo "  (无法连接)"
    fi

    if $running; then
        echo ""
        echo -e "  ${CYAN}─ 访问地址 ──────────────────────────────${NC}"
        echo -e "  管理后台: http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'SERVER_IP'):${PORT}/admin"
        echo -e "  API 地址: http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'SERVER_IP'):${PORT}/v1"
    fi
    echo ""
}

# ============================================================
# 13. 查看日志
# ============================================================
show_logs() {
    local lines=${1:-50}

    if systemctl is-active --quiet "$APP_NAME" 2>/dev/null; then
        journalctl -u "$APP_NAME" -n "$lines" --no-pager
        return
    fi

    if [ -f "$LOG_FILE" ]; then
        tail -n "$lines" "$LOG_FILE"
    else
        warn "日志文件不存在"
    fi
}

# ============================================================
# 14. 卸载
# ============================================================
uninstall() {
    warn "这将删除 Vibry AI Core 的所有文件和配置!"
    echo -n "确认卸载? 输入 yes 继续: "
    read -r confirm
    if [ "$confirm" != "yes" ]; then
        log "已取消"
        exit 0
    fi

    stop_server
    systemctl disable "$APP_NAME" 2>/dev/null || true
    rm -f "$SERVICE_FILE"
    rm -f "${NGINX_CONF}.conf"
    rm -f "/etc/nginx/sites-enabled/${APP_NAME}.conf"
    systemctl daemon-reload 2>/dev/null || true

    # 备份 .env 和数据库
    if [ -f "$ENV_FILE" ] || [ -f "$APP_DIR/vibrycard.db" ]; then
        BACKUP_DIR="/tmp/vibry-backup-$(date +%Y%m%d-%H%M%S)"
        mkdir -p "$BACKUP_DIR"
        cp "$ENV_FILE" "$BACKUP_DIR/" 2>/dev/null || true
        cp "$APP_DIR"/*.db "$BACKUP_DIR/" 2>/dev/null || true
        log "配置和数据库已备份到: $BACKUP_DIR"
    fi

    rm -rf "$APP_DIR"
    log "Vibry AI Core 已卸载"
}

# ============================================================
# 15. 完整首次部署
# ============================================================
full_deploy() {
    echo ""
    echo -e "${GREEN}╔══════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║     Vibry AI Core — Linux 一键部署               ║${NC}"
    echo -e "${GREEN}║     Digital Prefrontal Cortex Memory Proxy       ║${NC}"
    echo -e "${GREEN}╚══════════════════════════════════════════════════╝${NC}"
    echo ""

    must_be_root

    install_system_deps
    setup_user_and_dirs
    deploy_code
    setup_venv
    setup_env
    setup_systemd
    setup_firewall
    fix_permissions
    start_server

    # 如果设置了域名，自动配置 Nginx
    if [ -n "$DOMAIN" ]; then
        setup_nginx "$DOMAIN"
    fi

    echo ""
    echo -e "${GREEN}╔══════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║  ✅ 部署完成！                                   ║${NC}"
    echo -e "${GREEN}╠══════════════════════════════════════════════════╣${NC}"
    echo -e "${GREEN}║                                                  ${NC}"
    echo -e "${GREEN}║  安装路径: ${APP_DIR}${NC}"
    if [ -n "$DOMAIN" ]; then
    echo -e "${GREEN}║  域名:     https://${DOMAIN}${NC}"
    fi
    echo -e "${GREEN}║  管理后台: http://<服务器IP>:${PORT}/admin         ${NC}"
    echo -e "${GREEN}║  API 地址: http://<服务器IP>:${PORT}/v1            ${NC}"
    echo -e "${GREEN}║                                                  ${NC}"
    echo -e "${GREEN}║  后续步骤:                                       ${NC}"
    echo -e "${GREEN}║  1. 编辑配置: nano ${ENV_FILE}                    ${NC}"
    echo -e "${GREEN}║  2. 重启服务: sudo bash ${APP_DIR}/deploy.sh --restart${NC}"
    if [ -z "$DOMAIN" ]; then
    echo -e "${GREEN}║  3. 配置域名: sudo bash ${APP_DIR}/deploy.sh --nginx your-domain.com${NC}"
    fi
    echo -e "${GREEN}║                                                  ${NC}"
    echo -e "${GREEN}║  管理命令:                                       ${NC}"
    echo -e "${GREEN}║    systemctl status ${APP_NAME}    # 查看状态      ${NC}"
    echo -e "${GREEN}║    systemctl restart ${APP_NAME}   # 重启          ${NC}"
    echo -e "${GREEN}║    journalctl -u ${APP_NAME} -f   # 实时日志       ${NC}"
    echo -e "${GREEN}║                                                  ${NC}"
    echo -e "${GREEN}╚══════════════════════════════════════════════════╝${NC}"
    echo ""
}

# ============================================================
# 主入口
# ============================================================

# 如果脚本不在 APP_DIR（首次从解压目录运行），不需要 root 来查看帮助
case "${1:-}" in
    --help|-h)
        echo "Vibry AI Core — 部署 & 管理脚本"
        echo ""
        echo "用法: sudo bash deploy.sh [选项]"
        echo ""
        echo "  无参数                  完整首次部署"
        echo "  --update                从新版本目录全量更新，保留数据并支持失败回滚"
        echo "  --start                 启动服务"
        echo "  --stop                  停止服务"
        echo "  --restart               重启服务"
        echo "  --status                查看运行状态"
        echo "  --logs [N]              查看最近 N 行日志 (默认50)"
        echo "  --nginx [domain]        生成 Nginx 反向代理配置"
        echo "  --uninstall             完全卸载 (会提示确认)"
        echo ""
        echo "环境变量:"
        echo "  VIBRY_HOME=/opt/http/vibryai/server  自定义安装路径"
        echo "  VIBRY_PORT=9999                      自定义端口"
        echo "  VIBRY_DOMAIN=163.7.8.8               配置 Nginx 访问地址"
        echo "  VIBRY_USER=vibry                     运行用户"
        echo ""
        echo "示例 (自定义路径+访问地址):"
        echo "  sudo VIBRY_HOME=/opt/http/vibryai/server VIBRY_DOMAIN=163.7.8.8 bash deploy.sh"
        echo ""
        echo "部署流程:"
        echo "  1. 本地打包:   bash package.sh"
        echo "  2. 上传:       scp release/vibry-server-*.tar.gz user@server:/tmp/"
        echo "  3. 解压:       ssh user@server 'cd /tmp && tar -xzf vibry-server-*.tar.gz'"
        echo "  4. 部署:       ssh user@server 'cd /tmp/vibry-server-* && sudo VIBRY_HOME=/opt/http/vibryai/server VIBRY_DOMAIN=163.7.8.8 bash deploy.sh --update'"
        echo "  5. 编辑配置:   sudo nano /opt/http/vibryai/server/.env"
        echo "  6. 重启生效:   sudo bash /opt/http/vibryai/server/deploy.sh --restart"
        exit 0
        ;;
    --start)
        start_server
        exit 0
        ;;
    --stop)
        stop_server
        exit 0
        ;;
    --restart)
        stop_server
        sleep 1
        start_server
        exit 0
        ;;
    --status)
        show_status
        exit 0
        ;;
    --logs)
        show_logs "${2:-50}"
        exit 0
        ;;
    --nginx)
        setup_nginx "${2:-}"
        exit 0
        ;;
    --uninstall)
        uninstall
        exit 0
        ;;
    --update)
        update_release
        exit 0
        ;;
    "")
        full_deploy
        ;;
    *)
        err "未知选项: $1 (用 --help 查看帮助)"
        ;;
esac
