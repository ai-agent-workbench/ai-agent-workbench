#!/usr/bin/env bash
# codex-web 健康探活：探测本地 /login（免登录 200）。
# 连续 HEALTHCHECK_MAX_FAILS 次失败才发一条 ntfy 告警（容忍发布重启的秒级中断与短时抖动）；
# 恢复后补发一条恢复通知，随后归零重新计数。
# 附带磁盘空间预警（2026-07-31 新增）：WSL 根分区使用率 + 宿主 C:/D: 剩余空间越限即发一条
# ntfy 告警（仅一次），恢复后补通知。背景：07-30 vhdx 滚雪球撑爆宿主盘事故——WSL 内看不到
# 宿主真实余量，但 /mnt/c、/mnt/d 经 9p 挂载可见，每 5 分钟随本脚本顺带检查。
# 通知主题读取现有配置文件，不落任何文档。仅供 systemd timer 与运维通道调用。
set -uo pipefail

URL=${HEALTHCHECK_URL:-http://127.0.0.1:${CODEX_WEB_PORT:-18080}/login}
STATE_DIR=${HEALTHCHECK_STATE_DIR:-${CODEX_WEB_STATE_DIR:-$HOME/.local/share/codex-web}}
TOPIC_FILE=${HEALTHCHECK_TOPIC_FILE:-$STATE_DIR/ntfy-topic.txt}
MAX_FAILS=${HEALTHCHECK_MAX_FAILS:-3}
TIMEOUT=${HEALTHCHECK_TIMEOUT:-10}
DRY_RUN=${HEALTHCHECK_DRY_RUN:-0}
DISK_WARN_PERCENT=${HEALTHCHECK_DISK_WARN_PERCENT:-85}
HOST_MIN_FREE_GB=${HEALTHCHECK_HOST_MIN_FREE_GB:-50}

FAIL_FILE="$STATE_DIR/health-check-fails"
ALERTED_FILE="$STATE_DIR/health-check-alerted"
DISK_ALERTED_FILE="$STATE_DIR/disk-check-alerted"

rfc2047() {
    # 请求头仅 latin-1，中文标题按 RFC 2047 编码（与 shared/security.py 同款；07-22 曾因此静默丢通知）
    printf '=?UTF-8?B?%s?=' "$(printf '%s' "$1" | base64 -w0)"
}

send_ntfy() {  # $1=标题 $2=优先级 $3=正文
    if [[ "$DRY_RUN" == "1" ]]; then
        printf '[dry-run] ntfy title="%s" priority=%s body="%s"\n' "$1" "$2" "$3"
        return 0
    fi
    [[ -r "$TOPIC_FILE" ]] || return 1
    local topic
    topic=$(tr -d '[:space:]' < "$TOPIC_FILE")
    [[ -n "$topic" ]] || return 1
    curl -sS -m 15 -o /dev/null \
        -H "Title: $(rfc2047 "$1")" -H "Priority: $2" -H 'Tags: warning' \
        --data-binary "$3" "https://ntfy.sh/$topic"
}

if [[ "${1:-}" == "--test-alert" ]]; then
    send_ntfy '工作台健康检查测试' 'default' '收到本消息即说明健康检查 ntfy 告警通道正常（手动测试）。'
    exit $?
fi

mkdir -p "$STATE_DIR"

check_disks() {  # 磁盘空间预警：WSL 根分区 + 宿主盘（vhdx 所在 D: 及 C:）
    local problems=()
    local root_use
    root_use=$(df -P / | awk 'NR==2 {gsub(/%/,"",$5); print $5}')
    if [[ "$root_use" =~ ^[0-9]+$ ]] && (( root_use >= DISK_WARN_PERCENT )); then
        problems+=("WSL 根分区使用率 ${root_use}%（阈值 ${DISK_WARN_PERCENT}%）")
    fi
    local mnt label use free_gb
    for mnt in /mnt/c /mnt/d; do
        [[ -d "$mnt" ]] || continue
        label=${mnt#/mnt/}
        read -r use free_gb < <(df -P -BG "$mnt" | awk 'NR==2 {gsub(/%/,"",$5); gsub(/G/,"",$4); print $5, $4}')
        [[ "$use" =~ ^[0-9]+$ && "$free_gb" =~ ^[0-9]+$ ]] || continue
        if (( use >= DISK_WARN_PERCENT )) || (( free_gb < HOST_MIN_FREE_GB )); then
            problems+=("宿主 ${label}: 盘使用率 ${use}%、剩余 ${free_gb}G（阈值 ${DISK_WARN_PERCENT}% / 剩余 ${HOST_MIN_FREE_GB}G）")
        fi
    done
    if (( ${#problems[@]} > 0 )); then
        if [[ ! -f "$DISK_ALERTED_FILE" ]]; then
            local body
            body=$(printf '%s；' "${problems[@]}")
            if send_ntfy '磁盘空间预警' 'high' "磁盘空间越限：${body}。请清理或扩容，避免重蹈 07-30 vhdx 撑爆宿主盘事故。"; then
                touch "$DISK_ALERTED_FILE"
            fi
        fi
    elif [[ -f "$DISK_ALERTED_FILE" ]]; then
        send_ntfy '磁盘空间已恢复' 'default' 'WSL 根分区与宿主盘余量均回到阈值内。'
        rm -f "$DISK_ALERTED_FILE"
    fi
}
check_disks

fails=0
[[ -f "$FAIL_FILE" ]] && read -r fails < "$FAIL_FILE"
[[ "$fails" =~ ^[0-9]+$ ]] || fails=0

code=$(curl -sS -m "$TIMEOUT" -o /dev/null -w '%{http_code}' "$URL" 2>/dev/null || true)
[[ -n "$code" ]] || code=000

if [[ "$code" == 200 ]]; then
    if [[ -f "$ALERTED_FILE" ]]; then
        send_ntfy '工作台服务已恢复' 'default' '健康探活恢复：本地 /login 重新返回 200。'
        rm -f "$ALERTED_FILE"
    fi
    printf '0\n' > "$FAIL_FILE"
    exit 0
fi

fails=$((fails + 1))
printf '%s\n' "$fails" > "$FAIL_FILE"
printf 'health-check: probe %s -> %s, consecutive failures=%s\n' "$URL" "$code" "$fails" >&2

if (( fails >= MAX_FAILS )) && [[ ! -f "$ALERTED_FILE" ]]; then
    if send_ntfy '工作台服务无响应' 'high' "健康探活连续 ${fails} 次失败（返回 ${code}）：应用挂死或未运行，请检查。"; then
        touch "$ALERTED_FILE"
    fi
fi
exit 0
