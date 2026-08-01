#!/usr/bin/env bash
# 降级发布：无 sudo 时在服务外（如 SSH 会话）使用。
# py_compile → 快照 → 写 pending-publish 标记 → kill uvicorn → 等重启 → smoke_test → 失败回滚到上一份快照。
# 任务进程内禁止使用本脚本（KillMode=control-group 会连坐杀死脚本，冒烟/回滚丢失）；
# 任务内一律走 scripts/publish-deferred.sh。
# 用法：bash scripts/publish-degraded.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
STATE_DIR=${CODEX_WEB_STATE_DIR:-$HOME/.local/share/codex-web}
RELEASE_DIR="$STATE_DIR/releases"
STAMP=$(date +%Y%m%d-%H%M%S)
SNAPSHOT="$RELEASE_DIR/$STAMP"
MARKER="$RELEASE_DIR/pending-publish"
VENV_PYTHON="$APP_DIR/.venv/bin/python"
CODE_DIRS="routes shared engine middleware templates static"

mkdir -p "$RELEASE_DIR"
# 上一份快照（本次发布失败时的回滚源；必须在创建新快照之前取）
PREV=$(find "$RELEASE_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort -r | head -1 || true)

echo "=== 降级发布 $STAMP ==="

# 1. py_compile 全部 .py —— 任何语法错误在此中止，不杀旧进程
echo "[1/5] py_compile ..."
find "$APP_DIR" -maxdepth 2 -name '*.py' \
    -not -path '*/.venv/*' -not -path '*/__pycache__/*' -print0 \
    | xargs -0 "$VENV_PYTHON" -m py_compile
echo "  py_compile 通过"

# 2. 创建快照（记录的是本次要发布的新代码，供审计与版本留存）
echo "[2/5] 创建快照 ..."
mkdir -p "$SNAPSHOT/app"
chmod 700 "$RELEASE_DIR" "$SNAPSHOT"
cp "$APP_DIR/app.py" "$APP_DIR/tasks.py" "$APP_DIR/projects.py" \
   "$APP_DIR/security_monitor.py" "$APP_DIR/windows_bridge.py" \
   "$APP_DIR/model_catalog.py" "$APP_DIR/smoke_test.sh" "$SNAPSHOT/app/"
for d in $CODE_DIRS; do
    [[ -d "$APP_DIR/$d" ]] && cp -a "$APP_DIR/$d" "$SNAPSHOT/app/"
done
printf '%s\n' "$STAMP" > "$SNAPSHOT/version"

# 2.5 写发布标记：新进程启动钩子会复核冒烟，作为本脚本被杀时的兜底
printf '%s\n%s\n' "$STAMP" "$PREV" > "$MARKER"

# 3. kill 旧 uvicorn
echo "[3/5] 杀旧进程 ..."
uvpid=$(pgrep -f 'uvicorn app:app' | head -1)
if [[ -z "$uvpid" ]]; then
    echo "  未找到 uvicorn 进程，跳过 kill"
else
    kill -9 "$uvpid"
    echo "  已 kill $uvpid"
fi

# 4. 等 systemd 拉起
echo "[4/5] 等 systemd 拉起 ..."
for _ in $(seq 1 30); do
    if curl -fs -o /dev/null http://127.0.0.1:${CODEX_WEB_PORT:-18080}/login; then
        echo "  服务已恢复"
        break
    fi
    sleep 1
done

# 5. smoke_test
echo "[5/5] smoke_test ..."
if "$APP_DIR/smoke_test.sh"; then
    rm -f "$MARKER"
    echo "release=$STAMP status=healthy"
    find "$RELEASE_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' \
        | sort -nr | tail -n +11 | cut -d' ' -f2- | xargs -r rm -rf
    exit 0
fi

# 冒烟失败 → 回滚到上一份快照（注意：本脚本创建的快照是新代码，不能作为回滚源）
echo "smoke_test 失败，回滚到上一份快照 ${PREV:-（无）} ..."
if [[ -n "$PREV" && -d "$RELEASE_DIR/$PREV/app" ]]; then
    cp "$RELEASE_DIR/$PREV/app/"*.py "$APP_DIR/"
    cp "$RELEASE_DIR/$PREV/app/smoke_test.sh" "$APP_DIR/"
    for d in $CODE_DIRS; do
        if [[ -d "$RELEASE_DIR/$PREV/app/$d" ]]; then
            rm -rf "${APP_DIR:?}/$d"
            cp -a "$RELEASE_DIR/$PREV/app/$d" "$APP_DIR/$d"
        fi
    done
else
    echo "无可用旧快照，无法回滚；保留当前代码" >&2
fi

uvpid=$(pgrep -f 'uvicorn app:app' | head -1)
[[ -n "$uvpid" ]] && kill -9 "$uvpid"
for _ in $(seq 1 30); do
    if curl -fs -o /dev/null http://127.0.0.1:${CODEX_WEB_PORT:-18080}/login; then
        echo "status=rolledback"
        exit 1
    fi
    sleep 1
done
echo "status=rollback_restart_failed"
exit 1
