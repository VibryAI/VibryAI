#!/bin/bash
set -e

# ============================================================
# Vibry AI Core — 打包脚本
# 在本地/开发机运行，生成可直接部署到 Linux 的 tar.gz
# 用法:
#   bash package.sh              # 生成带时间戳的包
#   bash package.sh v1.2.0       # 指定版本号
# ============================================================

VERSION="${1:-$(date +%Y%m%d-%H%M%S)}"
PACKAGE_NAME="vibry-server-${VERSION}"
OUTPUT_DIR="${OUTPUT_DIR:-./release}"
PACKAGE_FILE="${OUTPUT_DIR}/${PACKAGE_NAME}.tar.gz"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'
log()  { echo -e "${GREEN}[PACK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }

# ---- 检查 ----
if [ ! -f "run.py" ]; then
    echo "[ERROR] 请在项目根目录运行此脚本"
    exit 1
fi

log "打包 Vibry AI Core v${VERSION}"

# ---- 清理 ----
log "清理临时文件..."
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find . -type f -name "*.pyc" -delete 2>/dev/null || true

# ---- 创建输出目录 ----
mkdir -p "$OUTPUT_DIR"

# ---- 打包 ----
log "创建 ${PACKAGE_FILE} ..."

tar -czf "$PACKAGE_FILE" \
    --exclude='.git' \
    --exclude='.gitignore' \
    --exclude='.claude' \
    --exclude='venv' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='*.db' \
    --exclude='*.db-shm' \
    --exclude='*.db-wal' \
    --exclude='data' \
    --exclude='ffmpeg-win-x86_64-v7.1.exe' \
    --exclude='nssm.exe' \
    --exclude='nssm.zip' \
    --exclude='*.log' \
    --exclude='nul' \
    --exclude='.env' \
    --exclude='push.sh' \
    --exclude='run.bat' \
    --exclude='deploy.bat' \
    --exclude='service.py' \
    --exclude='_admin_test.py' \
    --exclude='_quick_test.py' \
    --exclude='release' \
    --exclude='data' \
    --exclude='.DS_Store' \
    --exclude='Thumbs.db' \
    --transform "s,^\.,${PACKAGE_NAME}," \
    .

# ---- 校验 ----
SIZE=$(du -h "$PACKAGE_FILE" | cut -f1)
log "================================================"
log "  打包完成！"
log "  文件: ${PACKAGE_FILE}"
log "  大小: ${SIZE}"
log ""
log "  上传到服务器:"
log "    scp ${PACKAGE_FILE} user@your-server:/tmp/"
log ""
log "  在服务器上部署:"
log "    ssh user@your-server"
log "    cd /tmp"
log "    tar -xzf ${PACKAGE_NAME}.tar.gz"
log "    cd ${PACKAGE_NAME}"
log "    sudo bash deploy.sh"
log "================================================"
