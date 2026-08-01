#!/usr/bin/env bash
set -euo pipefail

APP_DIR=${CODEX_WEB_APP_DIR:-$(cd "$(dirname "$0")" && pwd)}
STATE_DIR=${CODEX_WEB_STATE_DIR:-$HOME/.local/share/codex-web}
TASKS_DIR="$STATE_DIR/tasks"
INBOX_DIR=${CODEX_WEB_INBOX_DIR:-$HOME/Inbox}
BASE_URL=${1:-http://127.0.0.1:${CODEX_WEB_PORT:-18080}}
# GeoIP 放行测试用的中国来源 IP（任一中国 IP 即可，非真实用户地址）
GEOIP_TEST_IP=${SMOKE_GEOIP_TEST_IP:-1.2.4.8}
# WSL 内部网关地址（nginx 回环反代）
GATEWAY_URL=${SMOKE_GATEWAY_URL:-http://127.0.0.1:19443}
before_drafts=$(find "$TASKS_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort)

cleanup() {
    after_drafts=$(find "$TASKS_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort)
    comm -13 <(printf '%s\n' "$before_drafts") <(printf '%s\n' "$after_drafts") | while read -r task_id; do
        [[ -n "$task_id" ]] || continue
        status=$(sed -n 's/.*"status": "\([^"]*\)".*/\1/p' "$TASKS_DIR/$task_id/task.json" 2>/dev/null)
        if [[ "$status" == draft ]]; then
            rm -rf "$TASKS_DIR/$task_id" "$INBOX_DIR/conversations/$task_id"
        fi
    done
}
trap cleanup EXIT

cookie=$(cd "$APP_DIR" && .venv/bin/python -c 'from shared.config import ACCOUNT; from shared.security import signer; print(signer().dumps(ACCOUNT))')
cookie_header="codex_web_session=$cookie"

contains() {
    local pattern=$1
    local file=$2
    if command -v rg >/dev/null 2>&1; then
        rg -q "$pattern" "$file"
    else
        grep -q "$pattern" "$file"
    fi
}

check_page() {
    local path=$1
    local expected=$2
    local output
    output=$(mktemp)
    code=$(curl -sS -o "$output" -w '%{http_code}' --cookie "$cookie_header" "$BASE_URL$path")
    [[ "$code" == 200 ]]
    contains "$expected" "$output"
    rm -f "$output"
}

check_page /home '今天想继续什么'
check_page /quick 'composer-config-panel'
check_page /projects '新建项目'
check_page /projects/history '历史对话'
check_page /settings '设置与安全'
check_page /inbox '收件箱整理'
check_page /manifest.webmanifest 'standalone'

# PWA 资源必须免登录可访问：安装、启动画面和 Service Worker 由系统直接请求，不带会话。
for path in manifest.webmanifest sw.js static/icon-192.png static/icon-maskable-512.png; do
    code=$(curl -sS -o /dev/null -w '%{http_code}' "$BASE_URL/$path")
    [[ "$code" == 200 ]]
done

project_path=$(cd "$APP_DIR" && .venv/bin/python -c 'import projects,urllib.parse; items=projects.recent(); print("/project/" + urllib.parse.quote(items[0]["id"]) if items else "/project/new")')
check_page "$project_path" '模型、思考与执行权限'

quick_html=$(mktemp)
curl -fsS --cookie "$cookie_header" "$BASE_URL/quick" > "$quick_html"
  # 模型目录是动态的（CC-Switch 切通道即变），断言最高优先级模型渲染在页面上，
  # 不再硬编码具体模型名。
  model_slug=$(cd "$APP_DIR" && .venv/bin/python -c 'import model_catalog; m=model_catalog.get_models(); print(m[0]["slug"] if m else "")')
  [[ -n "$model_slug" ]]
  contains "$model_slug" "$quick_html"
contains "danger-full-access" "$quick_html"
contains "value='ultra'" "$quick_html"
rm -f "$quick_html"

cd "$APP_DIR"
.venv/bin/python - <<'PY'
import tasks
import tempfile
from pathlib import Path
events = '\n'.join([
    '{"type":"item.completed","item":{"type":"agent_message","text":"阶段答复"}}',
    '{"type":"item.started","item":{"type":"agent_message","text":"未完成"}}',
])
assert tasks.agent_messages(events) == ["阶段答复"]
with tempfile.TemporaryDirectory() as root:
    tasks.ROOT = Path(root)
    task = tasks.ROOT / "test"
    task.mkdir()
    (task / "output.jsonl").write_text(events + '\n', encoding="utf-8")
    assert tasks.task_agent_messages("test") == ["阶段答复"]
    (task / "task.json").write_text('{"id":"test","target":"wsl","status":"failed","created":"2026-01-01T00:00:00+00:00"}', encoding="utf-8")
    assert tasks.status_snapshot("test")["agent_message_count"] == 1

    # Kimi stream-json 格式也应被识别为阶段性回复
    # - text 内容直接显示
    # - 仅含 think 内容时显示“🤔 {think 摘要}”作为进度提示
    kimi_events = '\n'.join([
        '{"role":"assistant","content":[{"type":"think","think":"内部思考"}],"tool_calls":[{"type":"function","function":{"name":"ReadFile"}}]}',
        '{"role":"assistant","content":[{"type":"text","text":"收到，我先查看相关代码。"}]}',
        '{"role":"assistant","content":[{"type":"text","text":"找到原因了：缓存重建时 O(n²) 扫描。"}]}',
    ])
    assert tasks.agent_messages(kimi_events) == ["🤔 内部思考", "收到，我先查看相关代码。", "找到原因了：缓存重建时 O(n²) 扫描。"]
    task2 = tasks.ROOT / "test-kimi"
    task2.mkdir()
    (task2 / "output.jsonl").write_text(kimi_events + '\n', encoding="utf-8")
    (task2 / "task.json").write_text('{"id":"test-kimi","target":"wsl","status":"running","created":"2026-01-01T00:00:00+00:00","agent":"kimi"}', encoding="utf-8")
    assert tasks.task_agent_messages("test-kimi") == ["🤔 内部思考", "收到，我先查看相关代码。", "找到原因了：缓存重建时 O(n²) 扫描。"]
    assert tasks.status_snapshot("test-kimi")["agent_message_count"] == 3
    # summary/final 仍只取 text，避免把思考过程写入最终答复
    assert "找到原因了" in tasks.status_snapshot("test-kimi", include_details=True)["summary"]
    assert "内部思考" not in tasks.status_snapshot("test-kimi", include_details=True)["summary"]
PY

for path in home quick projects projects/history settings inbox task/test project/new; do
    code=$(curl -sS -o /dev/null -w '%{http_code}' -H "X-Forwarded-For: $GEOIP_TEST_IP" "$GATEWAY_URL/$path")
    [[ "$code" != 404 ]]
done

conversation_proxy_code=$(curl -sS -o /dev/null -w '%{http_code}' \
    -H "X-Forwarded-For: $GEOIP_TEST_IP" -X POST -F prompt=test \
    "$GATEWAY_URL/conversation/not-a-real-task/send")
[[ "$conversation_proxy_code" == 303 ]]

conversation_code=$(curl -sS -o /dev/null -w '%{http_code}' --cookie "$cookie_header" \
    -X POST -F prompt=test "$BASE_URL/conversation/not-a-real-task/send")
[[ "$conversation_code" == 200 ]]

printf 'smoke_test=passed\n'
