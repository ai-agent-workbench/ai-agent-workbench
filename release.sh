#!/usr/bin/env bash
set -euo pipefail

APP_DIR=${CODEX_WEB_APP_DIR:-$(cd "$(dirname "$0")" && pwd)}
STATE_DIR=${CODEX_WEB_STATE_DIR:-$HOME/.local/share/codex-web}
RELEASE_DIR="$STATE_DIR/releases"
STAMP=$(date +%Y%m%d-%H%M%S)
SNAPSHOT="$RELEASE_DIR/$STAMP"

mkdir -p "$SNAPSHOT/app"
chmod 700 "$RELEASE_DIR" "$SNAPSHOT"
cp "$APP_DIR/app.py" "$APP_DIR/tasks.py" "$APP_DIR/projects.py" "$APP_DIR/security_monitor.py" "$APP_DIR/windows_bridge.py" "$APP_DIR/model_catalog.py" "$APP_DIR/smoke_test.sh" "$SNAPSHOT/app/"
cp -a "$APP_DIR/static" "$SNAPSHOT/app/static"
sudo cp /etc/systemd/system/codex-web.service "$SNAPSHOT/codex-web.service"
sudo cp /etc/systemd/system/codex-web-security-monitor.service "$SNAPSHOT/codex-web-security-monitor.service"
printf '%s\n' "$STAMP" > "$SNAPSHOT/version"

rollback() {
    cp "$SNAPSHOT/app/"*.py "$APP_DIR/"
    cp "$SNAPSHOT/app/smoke_test.sh" "$APP_DIR/"
    mkdir -p "$APP_DIR/static"
    cp -a "$SNAPSHOT/app/static/." "$APP_DIR/static/"
    sudo cp "$SNAPSHOT/codex-web.service" /etc/systemd/system/codex-web.service
    sudo cp "$SNAPSHOT/codex-web-security-monitor.service" /etc/systemd/system/codex-web-security-monitor.service
    sudo systemctl daemon-reload
    sudo systemctl restart codex-web codex-web-security-monitor
}
trap 'rollback' ERR

"$APP_DIR/.venv/bin/python" -m py_compile "$APP_DIR/app.py" "$APP_DIR/tasks.py" "$APP_DIR/projects.py" "$APP_DIR/security_monitor.py" "$APP_DIR/windows_bridge.py" "$APP_DIR/model_catalog.py"
sudo systemctl daemon-reload
sudo systemctl restart codex-web codex-web-security-monitor

PORT=${CODEX_WEB_PORT:-18080}

for _ in $(seq 1 20); do
    if curl -fs -o /dev/null "http://127.0.0.1:${PORT}/login"; then
        "$APP_DIR/smoke_test.sh"
        printf 'release=%s status=healthy\n' "$STAMP"
        trap - ERR
        find "$RELEASE_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' \
            | sort -nr | tail -n +11 | cut -d' ' -f2- | xargs -r rm -rf
        exit 0
    fi
    sleep 0.5
done

echo "Health check failed; restoring $STAMP" >&2
false
