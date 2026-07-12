#!/bin/bash
set -e

# ============================================================
# Vibry AI Core — 增量部署脚本
# 只打包 git diff 变更文件，秒级上传部署
# ============================================================
# 用法:
#   bash inc_deploy.sh                    # 增量部署（自动对比上次部署）
#   bash inc_deploy.sh --dry-run          # 预览将部署哪些文件
#   bash inc_deploy.sh --since HEAD~3     # 从指定 commit 开始部署
#   bash inc_deploy.sh --no-restart       # 只更新文件，不重启服务
#   bash inc_deploy.sh --set-baseline     # 将当前 HEAD 标记为已部署（不实际部署）
# ============================================================

# ---- 服务器配置 ----
SERVER="${DEPLOY_SERVER:-root@163.7.8.8}"
SERVER_PATH="${DEPLOY_PATH:-/opt/http/vibryai/server}"
TMP_PATH="${DEPLOY_TMP:-/opt/tmp}"
SERVICE_NAME="${DEPLOY_SERVICE:-vibry-server}"

# ---- 本地配置 ----
BASELINE_FILE=".inc_deploy_baseline"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DRY_RUN=false
NO_RESTART=false
SET_BASELINE=false
SINCE=""
CURRENT_BASELINE=""   # 由 main() 设置，供 show_preview 使用

# ---- 颜色 ----
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'
log()  { echo -e "${GREEN}[INC]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()  { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
info() { echo -e "${CYAN}       $1${NC}"; }

# ---- 排除规则 (与 package.sh / deploy.sh rsync 保持一致) ----
EXCLUDE_PATTERNS=(
    '.git'
    '.gitignore'
    '.claude'
    '.zcode'
    'venv'
    '__pycache__'
    '*.pyc'
    '*.db'
    '*.db-shm'
    '*.db-wal'
    'qdrant_data'
    'voiceprints'
    'data'
    'mem0'
    'mem0.zip'
    'ffmpeg-win-*'
    'nssm.exe'
    'nssm.zip'
    'nssm.*'
    '*.bat'
    'service.py'
    '*.log'
    'nul'
    '.env'
    'push.sh'
    'run.bat'
    'deploy.bat'
    '_admin_test.py'
    '_quick_test.py'
    'release'
    '.DS_Store'
    'Thumbs.db'
    '.inc_deploy_baseline'
    'inc_deploy.sh'
    'package.sh'
    'audio_processing_server.py'
)

# ============================================================
# 解析参数
# ============================================================
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --dry-run)       DRY_RUN=true ;;
            --no-restart)    NO_RESTART=true ;;
            --set-baseline)  SET_BASELINE=true ;;
            --since)
                shift
                SINCE="$1"
                ;;
            --help|-h)
                echo "Vibry AI Core — 增量部署脚本"
                echo ""
                echo "用法: bash inc_deploy.sh [选项]"
                echo ""
                echo "  --dry-run        预览将部署的文件，不实际执行"
                echo "  --since <ref>    从指定 git ref 开始对比 (默认: 上次部署的 commit)"
                echo "  --no-restart     只更新文件，不重启服务"
                echo "  --set-baseline   将当前 HEAD 记录为已部署基线 (不实际部署)"
                echo "  --help           显示帮助"
                echo ""
                echo "环境变量:"
                echo "  DEPLOY_SERVER    服务器地址 (默认: root@163.7.8.8)"
                echo "  DEPLOY_PATH      部署路径 (默认: /opt/http/vibryai/server)"
                echo "  DEPLOY_TMP       临时上传路径 (默认: /opt/tmp)"
                echo "  DEPLOY_SERVICE   systemd 服务名 (默认: vibry-server)"
                echo ""
                echo "工作原理:"
                echo "  1. 对比 git diff <baseline>..HEAD 获取变更文件"
                echo "  2. 过滤排除项 (venv/db/log/缓存等)"
                echo "  3. 打包 → scp → 服务器解压覆盖"
                echo "  4. 如有 requirements.txt 变更 → pip install"
                echo "  5. 重启服务 + 健康检查"
                echo "  6. 记录当前 HEAD 为新的基线"
                exit 0
                ;;
            *)
                err "未知选项: $1 (用 --help 查看帮助)"
                ;;
        esac
        shift
    done
}

# ============================================================
# 获取基线 commit
# ============================================================
get_baseline() {
    if [ -n "$SINCE" ]; then
        # 用户指定了起始 ref
        if ! git rev-parse "$SINCE" >/dev/null 2>&1; then
            err "无效的 git ref: $SINCE"
        fi
        echo "$SINCE"
        return
    fi

    if [ -f "$SCRIPT_DIR/$BASELINE_FILE" ]; then
        local baseline
        baseline=$(cat "$SCRIPT_DIR/$BASELINE_FILE" 2>/dev/null || true)
        if [ -n "$baseline" ] && git rev-parse "$baseline" >/dev/null 2>&1; then
            echo "$baseline"
            return
        fi
    fi

    # 回退：用上一次的 commit
    echo "HEAD~1"
}

# ============================================================
# 获取变更文件列表
# ============================================================
get_changed_files() {
    local baseline="$1"

    cd "$SCRIPT_DIR"

    # 已跟踪文件的变更 (对比工作区 vs baseline，含 staged + unstaged)
    local tracked
    tracked=$(git diff --name-only "$baseline" 2>/dev/null || true)

    # 已暂存的新文件 (git diff 不包含 untracked)
    local staged_new
    staged_new=$(git diff --name-only --diff-filter=A --cached "$baseline" 2>/dev/null || true)

    # 未跟踪的新文件 (排除 .gitignore 中的)
    local untracked
    untracked=$(git ls-files --others --exclude-standard 2>/dev/null || true)

    # 合并
    local all_files
    all_files=$( (echo "$tracked"; echo "$staged_new"; echo "$untracked") | sort -u | grep -v '^$' || true )

    echo "$all_files"
}

# ============================================================
# 检查文件是否应被排除
# ============================================================
is_excluded() {
    local file="$1"
    for pattern in "${EXCLUDE_PATTERNS[@]}"; do
        # 支持通配符匹配
        if [[ "$file" == $pattern ]] || [[ "$(basename "$file")" == $pattern ]]; then
            return 0
        fi
        # 检查目录前缀匹配
        if [[ "$file" == $pattern/* ]] || [[ "$file" == $pattern ]]; then
            return 0
        fi
    done
    return 1
}

# ============================================================
# 过滤变更文件
# ============================================================
filter_files() {
    local input="$1"
    local tmp_out
    tmp_out=$(mktemp)
    local tmp_req
    tmp_req=$(mktemp)

    # 先输出到临时文件 (避免 here-string 在 Windows bash 下的兼容问题)
    echo "$input" > "$tmp_out"

    local filtered=""
    local has_req=false

    while IFS= read -r file; do
        [ -z "$file" ] && continue

        # 跳过已删除的文件
        if [ ! -f "$SCRIPT_DIR/$file" ] && [ ! -d "$SCRIPT_DIR/$file" ]; then
            continue
        fi

        # 排除
        if is_excluded "$file"; then
            continue
        fi

        if [ -z "$filtered" ]; then
            filtered="$file"
        else
            filtered="$filtered"$'\n'"$file"
        fi

        # 检查是否包含 requirements.txt
        [ "$file" = "requirements.txt" ] && has_req=true
    done < "$tmp_out"

    rm -f "$tmp_out" "$tmp_req"

    echo "$filtered"
    [ "$has_req" = true ] && echo "___REQUIREMENTS_CHANGED___"
}

# ============================================================
# 增量打包
# ============================================================
create_incremental_package() {
    local files="$1"
    local pkg_path="$2"

    # 将文件列表写入临时文件
    local file_list
    file_list=$(mktemp)
    echo "$files" | grep -v '^___REQUIREMENTS' > "$file_list"

    local count
    count=$(grep -c '.' "$file_list" 2>/dev/null || echo 0)

    log "打包 $count 个变更文件..."

    # 用 tar 打包（读取文件列表）
    tar -czf "$pkg_path" -T "$file_list" 2>/dev/null

    rm -f "$file_list"

    local size
    size=$(du -h "$pkg_path" | cut -f1)
    info "增量包: $pkg_path ($size, $count 个文件)"
}

# ============================================================
# 上传到服务器
# ============================================================
upload_package() {
    local pkg_path="$1"
    local pkg_name
    pkg_name=$(basename "$pkg_path")

    log "上传到 $SERVER:$TMP_PATH/ ..."
    scp "$pkg_path" "${SERVER}:${TMP_PATH}/${pkg_name}" 2>&1 | tail -1
    log "上传完成"

    echo "$pkg_name"
}

# ============================================================
# 远程部署
# ============================================================
remote_deploy() {
    local pkg_name="$1"
    local has_req_change="$2"

    log "远程部署..."

    local restart_cmd=""
    if [ "$NO_RESTART" = false ]; then
        restart_cmd="
echo '[INC] 重启服务...'
systemctl restart ${SERVICE_NAME}
sleep 2
systemctl is-active --quiet ${SERVICE_NAME} && echo '[INC] ✅ 服务已重启' || echo '[INC] ❌ 服务启动失败！检查: journalctl -u ${SERVICE_NAME} -n 30'
"
    fi

    local pip_cmd=""
    if [ "$has_req_change" = true ]; then
        pip_cmd="
echo '[INC] requirements.txt 已变更，安装新依赖...'
source ${SERVER_PATH}/venv/bin/activate
pip install -r ${SERVER_PATH}/requirements.txt -q
echo '[INC] 依赖更新完成'
"
    fi

    # 构建远程命令
    ssh "$SERVER" bash -s << REMOTE_SCRIPT
set -e

GREEN='\033[0;32m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "\${GREEN}[INC]\${NC} \$1"; }

PKG="${TMP_PATH}/${pkg_name}"
SRV="${SERVER_PATH}"

if [ ! -f "\$PKG" ]; then
    echo -e "\${RED}[INC] 包文件不存在: \$PKG\${NC}"
    exit 1
fi

# 解压覆盖
log "解压增量包 \$PKG → \$SRV ..."
tar -xzf "\$PKG" -C "\$SRV" 2>/dev/null

# 修复权限
chown -R vibry:vibry "\$SRV" 2>/dev/null || true
chmod 600 "\$SRV/.env" 2>/dev/null || true

# pip install (如果需要)
$pip_cmd

# 重启服务
$restart_cmd

# 清理临时包
rm -f "\$PKG"

# 健康检查
if command -v curl >/dev/null 2>&1; then
    echo ''
    log '健康检查...'
    curl -s --max-time 5 'http://127.0.0.1:9999/api/health' 2>/dev/null && echo '' || echo '(无响应 — 服务可能正在启动)'
fi

log '✅ 增量部署完成'
REMOTE_SCRIPT
}

# ============================================================
# 更新基线
# ============================================================
update_baseline() {
    local current_head
    current_head=$(git rev-parse HEAD)
    echo "$current_head" > "$SCRIPT_DIR/$BASELINE_FILE"
    log "基线已更新: ${current_head:0:7}"
}

# ============================================================
# 显示变更预览
# ============================================================
show_preview() {
    local files="$1"
    local has_req="$2"

    echo ""
    echo -e "${GREEN}════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  增量部署预览${NC}"
    echo -e "${GREEN}════════════════════════════════════════════════${NC}"

    local tmp_preview
    tmp_preview=$(mktemp)
    echo "$files" > "$tmp_preview"

    local count=0
    while IFS= read -r file; do
        [ -z "$file" ] && continue
        [[ "$file" == ___REQUIREMENTS* ]] && continue
        count=$((count + 1))
        local type="?"
        # 判断变更类型: 新增未跟踪(N) > 已提交的新文件(A) > 修改(M)
        if git ls-files --others --exclude-standard -- "$file" 2>/dev/null | grep -q '.'; then
            type="N"
        elif [ -n "$CURRENT_BASELINE" ] && git diff --name-only --diff-filter=A "$CURRENT_BASELINE" HEAD -- "$file" 2>/dev/null | grep -q '.'; then
            type="A"
        elif [ -n "$CURRENT_BASELINE" ] && git diff --name-only "$CURRENT_BASELINE" -- "$file" 2>/dev/null | grep -q '.'; then
            type="M"
        fi
        printf "  ${CYAN}[%s]${NC} %s\n" "$type" "$file"
    done < "$tmp_preview"

    rm -f "$tmp_preview"

    echo ""
    echo -e "  总计: ${GREEN}${count}${NC} 个文件"
    [ "$has_req" = true ] && echo -e "  ${YELLOW}⚠ requirements.txt 已变更，将执行 pip install${NC}"
    echo -e "${GREEN}════════════════════════════════════════════════${NC}"
    echo ""
}

# ============================================================
# 主流程
# ============================================================
main() {
    parse_args "$@"

    cd "$SCRIPT_DIR"

    # --set-baseline: 只记录基线
    if [ "$SET_BASELINE" = true ]; then
        update_baseline
        log "已将当前 HEAD 标记为已部署基线"
        exit 0
    fi

    # 检查环境
    if ! git rev-parse --git-dir >/dev/null 2>&1; then
        err "当前目录不是 git 仓库"
    fi

    if [ ! -f "run.py" ]; then
        err "请在项目根目录运行此脚本"
    fi

    # 获取基线
    local baseline
    baseline=$(get_baseline)
    CURRENT_BASELINE="$baseline"
    info "基线: $(git rev-parse "$baseline" | head -c 7) ($baseline)"

    # 获取变更文件
    local raw_files
    raw_files=$(get_changed_files "$baseline")

    if [ -z "$raw_files" ]; then
        log "没有变更文件，无需部署"
        exit 0
    fi

    # 过滤
    local filtered
    filtered=$(filter_files "$raw_files")
    local has_req_change=false

    # 检查是否有 requirements 变更标记
    if echo "$filtered" | grep -q '___REQUIREMENTS_CHANGED___' 2>/dev/null; then
        has_req_change=true
    fi

    # 提取纯文件列表 (grep -v 没匹配时返回 1，加 || true)
    local files_only
    files_only=$(echo "$filtered" | grep -v '___REQUIREMENTS' || true)

    if [ -z "$files_only" ]; then
        log "变更文件都被排除，无需部署"
        exit 0
    fi

    # --dry-run: 只预览
    if [ "$DRY_RUN" = true ]; then
        show_preview "$files_only" "$has_req_change"
        exit 0
    fi

    # 增量部署
    show_preview "$files_only" "$has_req_change"

    echo -n "确认部署? [Y/n] "
    read -r confirm
    if [ "$confirm" = "n" ] || [ "$confirm" = "N" ]; then
        log "已取消"
        exit 0
    fi

    local pkg_name="vibry-inc-$(date +%Y%m%d-%H%M%S).tar.gz"
    local pkg_path="/tmp/$pkg_name"

    create_incremental_package "$files_only" "$pkg_path"
    local uploaded
    uploaded=$(upload_package "$pkg_path")
    remote_deploy "$uploaded" "$has_req_change"
    update_baseline
    rm -f "$pkg_path"

    echo ""
    log "全部完成！"
}

main "$@"
