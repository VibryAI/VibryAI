#!/bin/bash
# ============================================================
# Vibry AI Server — 服务器增量同步脚本
# ============================================================
# 用途: 将本地代码 rsync 同步到远程服务器并重启服务
#
# 用法:
#   bash update_server.sh                     # 默认配置
#   bash update_server.sh root@1.2.3.4        # 指定服务器
#   VIBRY_PORT=8888 bash update_server.sh     # 自定义端口
#
# 服务器目录结构:
#   /opt/http/vibryai/server/        ← 代码 + venv
#   /opt/http/vibryai/server/data/   ← SQLite 数据库
#   /opt/http/vibryai/server/.env    ← 环境配置
#   systemd service: vibry-server
#   Nginx: 代理 127.0.0.1:9999
# ============================================================
set -e

# ---- 配置 ----
REMOTE_HOST="${VIBRY_REMOTE:-root@api.vibry.ai}"
REMOTE_DIR="${VIBRY_HOME:-/opt/http/vibryai/server}"
SERVICE_NAME="${VIBRY_SERVICE:-vibry-server}"
PORT="${VIBRY_PORT:-9999}"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

# 解析命令行参数
if [ -n "$1" ]; then REMOTE_HOST="$1"; fi

echo "══════════════════════════════════════════════════"
echo "  Vibry AI Server — 增量同步"
echo "══════════════════════════════════════════════════"
echo "  服务器:   $REMOTE_HOST"
echo "  远程目录: $REMOTE_DIR"
echo "  服务名:   $SERVICE_NAME"
echo "  端口:     $PORT"
echo ""

# ---- Step 1: 同步代码 ----
echo "📤 [1/4] rsync 同步代码..."
rsync -avz --delete \
    --exclude='venv' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='data' \
    --exclude='*.db' \
    --exclude='*.db-shm' \
    --exclude='*.db-wal' \
    --exclude='qdrant_data' \
    --exclude='voiceprints' \
    --exclude='.env' \
    --exclude='*.log' \
    --exclude='.pid' \
    --exclude='.git' \
    --exclude='release' \
    --exclude='debug' \
    --exclude='audio' \
    --exclude='raw' \
    --exclude='wiki' \
    --exclude='wiki-rag' \
    --exclude='.zcode' \
    --exclude='.claude' \
    --exclude='ffmpeg-win-*' \
    --exclude='nssm.*' \
    --exclude='*.bat' \
    --exclude='update_server.sh' \
    --exclude='package.sh' \
    --exclude='mem0' \
    --exclude='*.zip' \
    -e ssh \
    "$LOCAL_DIR/" "$REMOTE_HOST:$REMOTE_DIR/"
echo "✅ 代码同步完成"
echo ""

# ---- Step 2: 更新 Python 依赖 ----
echo "📦 [2/4] 检查 Python 依赖..."
ssh "$REMOTE_HOST" "cd $REMOTE_DIR && \
    source venv/bin/activate && \
    pip install -r requirements.txt --quiet 2>&1 | tail -3 || true"
echo "✅ 依赖检查完成"
echo ""

# ---- Step 3: 重启服务 ----
echo "🔄 [3/4] 重启服务..."
ssh "$REMOTE_HOST" "systemctl restart $SERVICE_NAME 2>/dev/null && \
    echo 'systemd 服务已重启' || \
    (cd $REMOTE_DIR && source venv/bin/activate && \
     pkill -f 'app.main:app' 2>/dev/null; sleep 1; \
     nohup python run.py > server_output.log 2>&1 & \
     echo 'nohup 方式已重启')"
sleep 3
echo "✅ 服务已重启"
echo ""

# ---- Step 4: 健康检查 ----
echo "🏥 [4/4] 健康检查..."
sleep 2
HEALTH=$(ssh "$REMOTE_HOST" "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORT/api/health 2>/dev/null || echo '000'")

if [ "$HEALTH" = "200" ]; then
    echo "✅ 服务健康 (HTTP 200)"
    echo ""
    ssh "$REMOTE_HOST" "curl -s http://127.0.0.1:$PORT/api/health 2>/dev/null" 2>/dev/null | \
        python -m json.tool 2>/dev/null || ssh "$REMOTE_HOST" "curl -s http://127.0.0.1:$PORT/api/health" 2>/dev/null
else
    echo "⚠️  健康检查异常 (HTTP $HEALTH)"
    echo ""
    echo "  排查命令:"
    echo "    ssh $REMOTE_HOST 'systemctl status $SERVICE_NAME'"
    echo "    ssh $REMOTE_HOST 'journalctl -u $SERVICE_NAME -n 30 --no-pager'"
    echo "    ssh $REMOTE_HOST 'tail -30 $REMOTE_DIR/server_output.log'"
fi

echo ""
echo "══════════════════════════════════════════════════"
echo "  管理后台: https://api.vibry.ai/admin"
echo "══════════════════════════════════════════════════"
