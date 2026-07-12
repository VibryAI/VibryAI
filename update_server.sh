#!/bin/bash
# ============================================================
# Vibry AI Server — 增量更新脚本
# ============================================================
# 用途: 将本地代码同步到远程服务器并重启服务
#
# 用法:
#   bash update_server.sh                    # 默认使用脚本中的服务器配置
#   bash update_server.sh user@host          # 指定服务器
#   bash update_server.sh user@host /path    # 指定服务器+路径
#
# 前提:
#   - 本地有 ssh 访问权限
#   - 服务器已部署 Vibry AI Server (deploy.sh)
#   - 服务器使用 systemd 管理服务
# ============================================================
set -e

# ---- 配置 (按需修改) ----
REMOTE_HOST="${VIBRY_REMOTE:-root@api.vibry.ai}"
REMOTE_DIR="${VIBRY_HOME:-/opt/http/vibryai/server}"
SERVICE_NAME="${VIBRY_SERVICE:-vibry-server}"

# 解析命令行参数
if [ -n "$1" ]; then REMOTE_HOST="$1"; fi
if [ -n "$2" ]; then REMOTE_DIR="$2"; fi

LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "══════════════════════════════════════════════════"
echo "  Vibry AI Server — 增量更新"
echo "══════════════════════════════════════════════════"
echo "  服务器:   $REMOTE_HOST"
echo "  远程目录: $REMOTE_DIR"
echo "  本地目录: $LOCAL_DIR"
echo ""

# ---- Step 1: 同步代码 (rsync, 排除本地文件) ----
echo "📤 [1/4] 同步代码..."
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
    --exclude='ffmpeg-win-*' \
    --exclude='nssm.*' \
    --exclude='*.bat' \
    --exclude='update_server.sh' \
    --exclude='package.sh' \
    -e ssh \
    "$LOCAL_DIR/" "$REMOTE_HOST:$REMOTE_DIR/"

echo "✅ 代码同步完成"
echo ""

# ---- Step 2: 更新 Python 依赖 (如果有变化) ----
echo "📦 [2/4] 检查 Python 依赖..."
ssh "$REMOTE_HOST" "cd $REMOTE_DIR && \
    source venv/bin/activate && \
    pip install -r requirements.txt --quiet 2>&1 | tail -3 || true"
echo "✅ 依赖检查完成"
echo ""

# ---- Step 3: 重启服务 ----
echo "🔄 [3/4] 重启服务..."
ssh "$REMOTE_HOST" "systemctl restart $SERVICE_NAME 2>/dev/null || \
    (cd $REMOTE_DIR && source venv/bin/activate && \
     pkill -f 'app.main:app' 2>/dev/null; \
     nohup python run.py > server_output.log 2>&1 &)"
sleep 2
echo "✅ 服务已重启"
echo ""

# ---- Step 4: 健康检查 ----
echo "🏥 [4/4] 健康检查..."
sleep 3
HEALTH=$(ssh "$REMOTE_HOST" "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:9999/api/health 2>/dev/null || echo '000'")

if [ "$HEALTH" = "200" ]; then
    echo "✅ 服务健康 (HTTP 200)"
    echo ""
    # 显示健康详情
    ssh "$REMOTE_HOST" "curl -s http://127.0.0.1:9999/api/health 2>/dev/null" | python -m json.tool 2>/dev/null || true
else
    echo "⚠️  健康检查异常 (HTTP $HEALTH)"
    echo "   查看日志: ssh $REMOTE_HOST 'journalctl -u $SERVICE_NAME -n 50 --no-pager'"
    echo "   或:       ssh $REMOTE_HOST 'tail -50 $REMOTE_DIR/server_output.log'"
fi

echo ""
echo "══════════════════════════════════════════════════"
echo "  更新完成!"
echo "  管理后台: https://api.vibry.ai/admin"
echo "══════════════════════════════════════════════════"
