"""Minor routes: notifications, settings, inbox, PWA assets, bridge."""
import json

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from shared.config import (
    FILE_ROOTS, INBOX_DIR, NTFY_TOPIC_FILE, SENSITIVE_PATHS,
    kimi_model_display_name,
)
import shared.html_lib as hlib
from shared.html_lib import html, nav, human_size
from shared.security import audit, require_auth

router = APIRouter()


@router.get("/notifications")
def notifications(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        topic = NTFY_TOPIC_FILE.read_text(encoding="ascii").strip()
    except OSError:
        return html("ntfy 通知",
            hlib.templates.get_template("notifications.html").render()
            + "<p class='error'>通知主题暂不可读。</p><a href='/home'>返回工作台</a>",
        )
    return html("ntfy 通知",
        hlib.templates.get_template("notifications.html").render(topic=topic),
    )


@router.get("/settings")
def settings(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import projects as _projects
    from urllib.parse import quote as _quote
    body = (
        nav()
        + "<h1>设置与安全</h1>"
        + "<div style='margin:12px 0 18px'>"
        + "<label style='display:block;margin-bottom:5px;font-weight:600;font-size:.9rem'>界面底色</label>"
        + "<select id='theme-select' onchange=\"localStorage.setItem('codex-theme',this.value);document.documentElement.setAttribute('data-theme',this.value)\" style='max-width:200px'>"
        + "<option value='deep'>深黑</option><option value='medium'>暗灰</option>"
        + "<option value='neutral'>中性</option><option value='light'>浅灰</option></select>"
        + "<script>(function(){var t=localStorage.getItem('codex-theme')||'deep',s=document.getElementById('theme-select');s&&(s.value=t)})();</script></div>"
        + "<section class='cards'>"
        + "<a class='card' href='/inbox'><b>收件箱整理</b><small>查看临时对话附件</small></a>"
        + "<a class='card' href='/notifications'><b>通知</b><small>查看 ntfy 订阅信息</small></a>"
        + "<a class='card' href='/files?path=" + _quote(str(_projects.ROOT)) + "'><b>项目目录</b><small>浏览所有项目文件</small></a>"
        + "</section>"
        + "<details><summary>高级信息</summary><p>新对话默认使用 WSL；需要 Windows 程序时可在高级选项中切换。任务最长运行 6 小时。</p></details>"
        + "<form action='/logout' method='post'><button class='secondary'>退出登录</button></form>"
    )
    return html("设置", body)


@router.get("/inbox")
def inbox_overview(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    from pathlib import Path
    from urllib.parse import quote
    import html as html_escape
    import tasks

    rows = []
    root = INBOX_DIR / "conversations"
    if root.exists():
        for directory in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not directory.is_dir():
                continue
            files = [item for item in directory.iterdir() if item.is_file()]
            size = sum(item.stat().st_size for item in files)
            try:
                metadata, _, _ = tasks.get(directory.name)
                title = metadata.get("title") or "未发送对话"
                link = "/task/" + directory.name if metadata.get("status") != "draft" else "/quick?draft=" + directory.name
            except Exception:
                title = "未知对话"
                link = "/files?path=" + quote(str(directory))
            rows.append(
                "<li><a class='task-title' href='" + link + "'>"
                + html_escape.escape(title) + "</a><small>"
                + str(len(files)) + " 个附件，" + str(size) + " bytes</small></li>"
            )
    loose = [item for item in INBOX_DIR.iterdir() if item.is_file()] if INBOX_DIR.is_dir() else []
    body = (
        nav() + "<h1>收件箱整理</h1>"
        + "<p>这里只列出临时对话附件，不会自动删除。</p>"
        + "<h2>按对话整理</h2><ul class='task-list'>"
        + ("".join(rows) or "<li>暂无对话附件。</li>")
        + "</ul><h2>未整理文件</h2><p>" + str(len(loose)) + " 个文件</p>"
        + "<a class='button secondary' href='/files?path=" + quote(str(INBOX_DIR)) + "'>打开收件箱</a>"
    )
    return html("收件箱整理", body)


@router.get("/manifest.webmanifest")
def web_manifest():
    content = {
        "name": "Codex 工作台",
        "short_name": "Codex",
        "start_url": "/home",
        "display": "standalone",
        "background_color": "#0d0d0d",
        "theme_color": "#0d0d0d",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/static/icon-maskable-192.png", "sizes": "192x192", "type": "image/png", "purpose": "maskable"},
            {"src": "/static/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    return Response(json.dumps(content, indent=2), media_type="application/manifest+json")


@router.get("/sw.js")
def service_worker():
    return Response(
        "self.addEventListener('install',e=>{self.skipWaiting()});"
        "self.addEventListener('activate',e=>{e.waitUntil(clients.claim())});"
        "self.addEventListener('fetch',e=>{e.respondWith(fetch(e.request))});",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/static/{filename:path}")
def static_asset(filename: str):
    from pathlib import Path
    STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
    path = (STATIC_DIR / filename).resolve()
    if not str(path).startswith(str(STATIC_DIR.resolve())):
        return Response("forbidden", status_code=403)
    if not path.is_file():
        return Response("not found", status_code=404)
    # no-cache = 每次回源校验（FileResponse 带 ETag/Last-Modified，未变则 304）。
    # 否则手机端长缓存会让已发布的 JS 修复长期不生效（2026-07-28 复盘）。
    return FileResponse(path, headers={"Cache-Control": "no-cache"})


@router.post("/bridge/mcp/{task_id}")
async def bridge_mcp_endpoint(request: Request, task_id: str):
    import windows_bridge
    body = await request.json()
    method = body.get("method")
    message_id = body.get("id")
    client_host = request.client.host if request.client else None
    if client_host not in ("127.0.0.1", "::1"):
        audit(request, "bridge_mcp_rejected_non_loopback", {"task_id": task_id, "host": client_host})
        return JSONResponse({"jsonrpc": "2.0", "error": {"code": -32000, "message": "Forbidden"}}, status_code=403)
    if method and method.startswith("notifications/"):
        return Response(status_code=202)
    try:
        server = windows_bridge.McpServer(task_id)
        result = server.handle(body)
        return JSONResponse({"jsonrpc": "2.0", "id": message_id, "result": result})
    except Exception as error:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": message_id, "error": {"code": -32603, "message": str(error)}},
            status_code=500,
        )
