"""Codex Web Workbench — FastAPI application entry point."""
from pathlib import Path

from fastapi import FastAPI

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

# ── Middleware ───────────────────────────────────────────────────────────────

from middleware.rate_limit import RateLimitMiddleware
app.add_middleware(RateLimitMiddleware)

# ── Route registration ──────────────────────────────────────────────────────

from routes.home import router as home_router
from routes.task import router as task_router
from routes.files import router as files_router
from routes.projects import router as projects_router
from routes.takeover import router as takeover_router
from routes.auth import router as auth_router
from routes.misc import router as misc_router
from routes.ws import router as ws_router

app.include_router(home_router)
app.include_router(task_router)
app.include_router(files_router)
app.include_router(projects_router)
app.include_router(takeover_router)
app.include_router(auth_router)
app.include_router(misc_router)
app.include_router(ws_router)


# ── Deferred publish verification ───────────────────────────────────────────
# scripts/publish-deferred.sh 的阶段2：新进程启动时若发现 pending-publish
# 标记，在后台线程跑 smoke_test；失败则从上一份快照回滚并自杀，由 systemd
# 用旧代码重新拉起。冒烟/回滚在新进程的 cgroup 里执行，不会再被
# "杀旧 uvicorn"连带杀死（KillMode=control-group）。

APP_DIR = Path(__file__).resolve().parent
import os as _os
RELEASES_DIR = Path(_os.environ.get(
    "CODEX_WEB_STATE_DIR",
    str(Path.home() / ".local" / "share" / "codex-web"),
)) / "releases"
CODEX_WEB_PORT = _os.environ.get("CODEX_WEB_PORT", "18080")
PUBLISH_CODE_ITEMS = (
    "app.py", "tasks.py", "projects.py", "security_monitor.py",
    "windows_bridge.py", "model_catalog.py", "smoke_test.sh",
    "routes", "shared", "engine", "middleware", "templates", "static",
)


def _restore_snapshot(snapshot_dir):
    import shutil
    for name in PUBLISH_CODE_ITEMS:
        src = snapshot_dir / "app" / name
        dst = APP_DIR / name
        if not src.exists():
            continue
        if src.is_dir():
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)


def _verify_pending_publish():
    import os
    import signal
    import subprocess
    import threading
    import time
    from datetime import datetime, timezone

    marker = RELEASES_DIR / "pending-publish"
    if not marker.exists():
        return

    def run():
        def log(message):
            try:
                with (RELEASES_DIR / "publish.log").open("a", encoding="utf-8") as fh:
                    fh.write(datetime.now(timezone.utc).isoformat() + " " + message + "\n")
            except OSError:
                pass

        def wait_http_ready(deadline_s=60):
            # uvicorn startup 事件返回不代表已开始 accept 连接，轮询到 /login 返回 200 再冒烟。
            import urllib.request
            end = time.monotonic() + deadline_s
            while time.monotonic() < end:
                try:
                    with urllib.request.urlopen("http://127.0.0.1:" + CODEX_WEB_PORT + "/login", timeout=3) as resp:
                        if resp.status == 200:
                            return True
                except Exception:
                    pass
                time.sleep(1)
            return False

        try:
            parts = marker.read_text(encoding="utf-8").split()
            stamp = parts[0] if parts else ""
            previous = parts[1] if len(parts) > 1 else ""
            output = ""
            ok = False
            if wait_http_ready():
                result = subprocess.run(
                    ["bash", str(APP_DIR / "smoke_test.sh")],
                    cwd=str(APP_DIR), capture_output=True, timeout=180,
                )
                ok = result.returncode == 0
                if not ok:
                    output = (result.stdout.decode(errors="replace")[-500:]
                              + " stderr=" + result.stderr.decode(errors="replace")[-500:])
            else:
                output = "127.0.0.1:" + CODEX_WEB_PORT + " 在 60s 内未就绪"
            if ok:
                marker.unlink(missing_ok=True)
                log("release=" + stamp + " status=healthy verify=startup-hook")
                return
            log("release=" + stamp + " status=smoke_failed output=" + output)
            snapshot = RELEASES_DIR / previous if previous else None
            if snapshot and (snapshot / "app").is_dir():
                _restore_snapshot(snapshot)
                marker.unlink(missing_ok=True)
                log("status=rolledback_to=" + previous + "，自杀重启以加载回滚代码")
                os.kill(os.getpid(), signal.SIGKILL)
            else:
                marker.unlink(missing_ok=True)
                log("status=rollback_unavailable（无旧快照），保留当前代码")
        except Exception as exc:  # 验证逻辑自身出错也不能拖垮启动
            marker.unlink(missing_ok=True)
            log("status=verify_error " + repr(exc))

    threading.Thread(target=run, daemon=True).start()


# ── Startup ─────────────────────────────────────────────────────────────────

@app.on_event("startup")
def startup_cleanup():
    import tasks
    from shared.file_utils import clean_staging
    from shared.security import notify_task_terminal

    tasks.clean_empty_drafts()
    tasks.recover_interrupted_windows_executions()
    # 重启后兜底唤醒挂起轮：祖先已是终态的排队轮立即续发（全局扫描幂等）
    tasks.kick_held_turns()
    tasks.set_terminal_callback(notify_task_terminal)
    clean_staging()
    _verify_pending_publish()
