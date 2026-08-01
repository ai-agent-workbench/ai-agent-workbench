"""跨 home 接管 CLI/web 版 kimi 历史会话。"""
import html as html_escape
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from shared.file_utils import workspace
from shared.html_lib import html, nav
from shared.security import audit, require_auth

router = APIRouter()


_TAKEOVER_NOTICE = (
    "<p class='notice'>接管后 agent 在当前项目目录工作，"
    "历史会话里的其他路径只是记忆。</p>"
)


def _format_updated_at(updated_at_ms):
    """把 state.json 的 updatedAt(毫秒) 转成本地 Y-m-d H:i。"""
    try:
        return datetime.fromtimestamp(int(updated_at_ms) / 1000).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return "未知"


def _short_cwd(cwd, limit=40):
    """缩短 cwd 显示；过长保留末尾。"""
    text = cwd or ""
    if len(text) <= limit:
        return html_escape.escape(text)
    return html_escape.escape("…" + text[-limit:])


@router.get("/takeover")
def takeover_list(request: Request, project_id: str = ""):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import projects, tasks
    if not project_id:
        return html("缺少项目",
            nav() + "<h1>缺少项目</h1><p class='error'>请先选择一个项目。</p><a href='/projects'>返回项目列表</a>",
        )
    try:
        project = projects.get(project_id)
    except Exception:
        return html("项目不存在",
            nav() + "<h1>项目不存在</h1><a href='/projects'>返回项目列表</a>",
        )
    sessions = tasks.list_external_kimi_sessions(limit=100)
    rows = []
    for session in sessions:
        title = session["title"]
        if len(title) > 60:
            title = title[:60] + "…"
        safe_title = html_escape.escape(title)
        safe_session_id = html_escape.escape(session["session_id"], quote=True)
        safe_project_id = html_escape.escape(project_id, quote=True)
        rows.append(
            "<li class='takeover-row'>"
            "<div class='takeover-info'>"
            "<b>" + safe_title + "</b><br>"
            "<small>" + _format_updated_at(session["updated_at"]) + " · "
            + _short_cwd(session["cwd"]) + "</small>"
            "</div>"
            "<form action='/takeover' method='post'>"
            "<input type='hidden' name='session_id' value='" + safe_session_id + "'>"
            "<input type='hidden' name='project_id' value='" + safe_project_id + "'>"
            "<button>接管</button></form></li>"
        )
    body = (
        nav() + "<h1>接管历史会话</h1>"
        + "<p>项目：<b>" + html_escape.escape(project["name"]) + "</b></p>"
        + _TAKEOVER_NOTICE
        + "<ul class='takeover-list'>"
        + ("".join(rows) or "<p><small>没有发现可接管的外部 kimi 会话。</small></p>")
        + "</ul>"
        + "<p><a href='/project/" + quote(project_id) + "'>返回项目</a></p>"
    )
    return html("接管历史会话", body)


@router.post("/takeover")
def takeover_action(request: Request, session_id: str = Form(...), project_id: str = Form(...)):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import projects, tasks
    try:
        project = projects.get(project_id)
        directory = workspace(project["path"], "wsl")
    except Exception:
        return html("项目无效",
            nav() + "<h1>项目无效</h1><a href='/projects'>返回项目列表</a>",
        )
    if not tasks.takeover_kimi_session(session_id):
        return html("接管失败",
            nav() + "<h1>接管失败</h1>"
            + "<p class='error'>无法接管该会话，请确认会话仍存在。</p>"
            + "<a href='/takeover?project_id=" + html_escape.escape(project_id, quote=True) + "'>返回</a>",
        )
    sessions = {s["session_id"]: s for s in tasks.list_external_kimi_sessions(limit=100)}
    session_info = sessions.get(session_id, {})
    session_title = session_info.get("title", "外部会话")
    display_title = session_title
    if len(display_title) > 40:
        display_title = display_title[:40] + "…"
    prompt = (
        "这是一个接管过来的历史会话。请用一两句话总结我们之前在讨论什么，然后等我指令。"
    )
    # model 不传：start() 会以 kimi_default_model() 兜底；
    # model_catalog.get_default_model() 是 codex 侧默认（实测 deepseek-v4-pro），误传会被 kimi 拒绝。
    metadata = tasks.create(
        "wsl", prompt, directory, project_id,
        permission="workspace-write",
        agent="kimi",
        title="接管：" + display_title,
    )
    # 预写 kimi_session_id，让 start() 的 ACP 分支按真实 session  resume
    metadata["kimi_session_id"] = session_id
    # 思考档延续原对话（2026-07-29 用户拍板：原对话什么模式，接管后还是什么模式）；
    # 收割不到或非三档值（如 K2.7 的 on）则不写，start() 以工作台默认 high 兜底。
    original_thinking = tasks.harvest_session_thinking(session_id)
    if original_thinking in {"low", "high", "max"}:
        metadata["thinking"] = original_thinking
    tasks._write_metadata(metadata["id"], metadata)
    tasks.start(metadata["id"])
    projects.touch(project_id, metadata["id"], metadata["prompt"])
    audit(request, "session_taken_over", {"task_id": metadata["id"], "session_id": session_id})
    return RedirectResponse("/task/" + metadata["id"], 303)
