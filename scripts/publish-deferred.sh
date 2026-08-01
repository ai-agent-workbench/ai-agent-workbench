#!/usr/bin/env bash
# 两阶段安全发布 —— 任务进程内唯一允许的发布路径。
#
# 背景：systemd 单元 KillMode=control-group，杀 uvicorn 会连坐杀死同 cgroup 的
# 一切进程（包括发布脚本与发起任务的 AI 进程）；本机无 cron 写权限、无 user bus、
# 无 at，无法从 cgroup 内"逃生"。因此拆成两阶段：
#
#   阶段1（本脚本，任务内执行）：py_compile → 快照 → 写 pending-publish 标记 →
#         延迟 N 秒 kill uvicorn。延迟是为了让调用方（AI 任务）正常收尾；
#         kill 之后本任务进程被杀属预期。
#   阶段2（新 uvicorn 启动钩子，app.py `_verify_pending_publish`）：
#         读取标记 → smoke_test → 失败则从上一份快照回滚并自杀，由 systemd
#         用旧代码重新拉起。冒烟/回滚在新 cgroup 执行，不会再被发布杀死。
#
# 结果查询：$CODEX_WEB_STATE_DIR/releases/publish.log
# 用法：bash scripts/publish-deferred.sh [延迟秒数，默认 60]
set -euo pipefail

DELAY="${1:-60}"
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
STATE_DIR=${CODEX_WEB_STATE_DIR:-$HOME/.local/share/codex-web}
RELEASE_DIR="$STATE_DIR/releases"
MARKER="$RELEASE_DIR/pending-publish"
LOCK="$RELEASE_DIR/.publish.lock"
LOG="$RELEASE_DIR/publish.log"
VENV_PYTHON="$APP_DIR/.venv/bin/python"
STAMP=$(date +%Y%m%d-%H%M%S)
SNAPSHOT="$RELEASE_DIR/$STAMP"
CODE_DIRS="routes shared engine middleware templates static"

mkdir -p "$RELEASE_DIR"
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "另一个发布正在进行，退出" >&2
    exit 1
fi
if [[ -e "$MARKER" ]]; then
    echo "已存在待验证的发布标记（pending-publish），请先确认上次发布结果" >&2
    exit 1
fi
# 上一份快照（回滚源；必须在创建新快照之前取）
PREV=$(find "$RELEASE_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort -r | head -1 || true)

echo "=== 两阶段延迟发布 $STAMP（回滚源 ${PREV:-无}） ===" | tee -a "$LOG"

# 1. py_compile 全部 .py —— 语法错误在此中止，不写标记、不杀进程
echo "[1/4] py_compile ..."
find "$APP_DIR" -maxdepth 2 -name '*.py' \
    -not -path '*/.venv/*' -not -path '*/__pycache__/*' -print0 \
    | xargs -0 "$VENV_PYTHON" -m py_compile
echo "  py_compile 通过"

# 2. 创建快照（新代码，供审计/留存；回滚源是上一份快照 PREV）
echo "[2/4] 创建快照 ..."
mkdir -p "$SNAPSHOT/app"
chmod 700 "$RELEASE_DIR" "$SNAPSHOT"
cp "$APP_DIR/app.py" "$APP_DIR/tasks.py" "$APP_DIR/projects.py" \
   "$APP_DIR/security_monitor.py" "$APP_DIR/windows_bridge.py" \
   "$APP_DIR/model_catalog.py" "$APP_DIR/smoke_test.sh" "$SNAPSHOT/app/"
for d in $CODE_DIRS; do
    [[ -d "$APP_DIR/$d" ]] && cp -a "$APP_DIR/$d" "$SNAPSHOT/app/"
done
printf '%s\n' "$STAMP" > "$SNAPSHOT/version"

# 3. 写发布标记（启动钩子的依据：新快照 stamp + 回滚源 prev）
echo "[3/4] 写发布标记 ..."
printf '%s\n%s\n' "$STAMP" "$PREV" > "$MARKER"

# 4. 延迟 kill：脱离会话的后台进程只负责递送 kill；杀完它自己随 cgroup 消亡也无妨
echo "[4/4] ${DELAY}s 后 kill uvicorn（本任务将随之中断，属预期） ..."
uvpid=$(pgrep -f 'uvicorn app:app' | head -1)
if [[ -z "$uvpid" ]]; then
    echo "  未找到 uvicorn 进程，取消发布并清除标记" | tee -a "$LOG" >&2
    rm -f "$MARKER"
    exit 1
fi
setsid nohup bash -c "sleep '$DELAY'; kill -9 '$uvpid'" >/dev/null 2>&1 &
echo "已调度：kill $uvpid @ +${DELAY}s；阶段2 由新进程启动钩子执行，结果见 $LOG" | tee -a "$LOG"
