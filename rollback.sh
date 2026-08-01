#!/usr/bin/env bash
set -euo pipefail

APP_DIR=${CODEX_WEB_APP_DIR:-$(cd "$(dirname "$0")" && pwd)}
STATE_DIR=${CODEX_WEB_STATE_DIR:-$HOME/.local/share/codex-web}
RELEASE_DIR="$STATE_DIR/releases"
SNAPSHOT=${1:-$(find "$RELEASE_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -nr | head -1 | cut -d' ' -f2-)}

if [[ -z "$SNAPSHOT" || ! -f "$SNAPSHOT/version" ]]; then
    echo "Usage: $0 [snapshot-directory]" >&2
    exit 2
fi

compile_files=("$SNAPSHOT/app/app.py" "$SNAPSHOT/app/tasks.py" "$SNAPSHOT/app/security_monitor.py")
if [[ -f "$SNAPSHOT/app/projects.py" ]]; then
    compile_files+=("$SNAPSHOT/app/projects.py")
fi
"$APP_DIR/.venv/bin/python" -m py_compile "${compile_files[@]}"
cp "$SNAPSHOT/app/"*.py "$APP_DIR/"
sudo cp "$SNAPSHOT/codex-web.service" /etc/systemd/system/codex-web.service
sudo cp "$SNAPSHOT/codex-web-security-monitor.service" /etc/systemd/system/codex-web-security-monitor.service
sudo systemctl daemon-reload
sudo systemctl restart codex-web codex-web-security-monitor

PORT=${CODEX_WEB_PORT:-18080}

for _ in $(seq 1 20); do
    if curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/login"; then
        printf 'rollback=%s status=healthy\n' "$(<"$SNAPSHOT/version")"
        exit 0
    fi
    sleep 0.5
done

echo "Rollback files were restored, but the health check failed." >&2
exit 1
