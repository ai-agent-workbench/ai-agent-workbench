"""Home page, quick-task, history, and conversation-management routes."""
import html as html_escape
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from shared.config import NTFY_TOPIC_FILE
import shared.html_lib as hlib
from shared.html_lib import (
    config_script, _conversation_groups, _conversation_card,
    compact_config_controls, html, management_script, nav,
    upload_dialog, upload_notice,
)
from shared.security import audit, require_auth
import model_catalog

router = APIRouter()


@router.get("/")
def home(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import tasks, projects
    roots = tasks.conversation_roots()[:8]
    project_cards = []
    for project in projects.recent()[:6]:
        project_cards.append(
            "<a class='card' href='/project/" + quote(project["id"]) + "'>"
            "<b>" + html_escape.escape(project["name"]) + "</b>"
            "<small>继续项目</small></a>"
        )
    conversation_section = _conversation_groups(roots, "home-manage")
    manage_bar = (
        "<form id='home-manage' class='manage-bar' action='/projects/history/manage' method='post'>"
        "<button type='button' class='secondary' data-all>全选</button>"
        "<button name='action' value='pin'>置顶</button>"
        "<button name='action' value='unpin' class='secondary'>取消置顶</button>"
        "<button name='action' value='delete' class='danger'>删除</button>"
        "<button type='button' class='secondary' data-cancel><span data-count>0 项</span> 取消</button>"
        "</form>"
    )
    page = (
        "<h1>今天想继续什么？</h1>"
        "<section class='cards'>"
        "<a class='card' href='/project/new'><b>＋ 新建项目</b><small>长期工作集中保存</small></a>"
        "<a class='card' href='/quick'><b>新对话</b><small>像聊天一样直接发送</small></a>"
        "</section>"
        "<h2>最近项目</h2>"
        "<section class='cards'>"
        + ("".join(project_cards) or "<p><small>还没有项目。新建一个项目后，资料和任务会集中保存。</small></p>")
        + "</section>"
        "<h2>最近任务</h2>"
        "<section class='task-list history-list'>"
        + (conversation_section or "<p><small>还没有对话。</small></p>")
        + "</section>"
    )
    body = (
        nav()
        + hlib.templates.get_template("generic_page.html").render(
            title="Codex 工作台", content=page, manage_bar=manage_bar,
        )
        + management_script("home-manage")
    )
    return html("Codex 工作台", body)


@router.get("/home")
def protected_home(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    return home(request)


@router.get("/quick")
def quick_task(request: Request, draft: str = "", uploaded: str = ""):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import tasks
    try:
        conversation = tasks.get_draft(draft) if draft else tasks.create_draft()
    except Exception:
        conversation = tasks.create_draft()
    return_to = "/quick?draft=" + conversation["id"]
    compact_config = compact_config_controls(
        "quick-send", model_catalog.get_default_model(), "medium",
        "workspace-write", "-quick",
    )
    body = (
        nav() + "<h1>新对话</h1>" + upload_notice(uploaded)
        + "<div class='chat-composer bottom-composer'>"
        + "<form id='quick-send' action='/conversation/"
        + conversation["id"] + "/send' method='post'></form>"
        + "<textarea form='quick-send' name='prompt' rows='6' required autofocus "
        + "placeholder='直接告诉 Codex 你想做什么'></textarea>"
        + "<div class='composer-compact-row'>"
        + compact_config
        + upload_dialog(conversation["workdir"], return_to, "quick-upload")
        + "<button class='refresh-button icon-button' type='button' "
        + "onclick='location.reload()' title='刷新'>↻</button>"
        + "<button class='send-button' form='quick-send'>发送</button>"
        + "</div></div>"
        + config_script()
        + "<script>if(new URLSearchParams(location.search).get('quoted')){"
        + "const quote=localStorage.getItem('codex-new-quote');"
        + "if(quote){document.querySelector(\"textarea[name='prompt']\").value="
        + "'引用：\\n'+quote+'\\n\\n';localStorage.removeItem('codex-new-quote');}}</script>"
    )
    return html("新对话", body)


@router.get("/history")
@router.get("/projects/history")
def history(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import tasks
    conversation_section = _conversation_groups(tasks.conversation_roots(), "history-manage")
    manage_bar = (
        "<form id='history-manage' class='manage-bar' action='/projects/history/manage' method='post'>"
        "<button type='button' class='secondary' data-all>全选</button>"
        "<button name='action' value='pin'>置顶</button>"
        "<button name='action' value='unpin' class='secondary'>取消置顶</button>"
        "<button name='action' value='delete' class='danger'>删除</button>"
        "<button type='button' class='secondary' data-cancel><span data-count>0 项</span> 取消</button>"
        "</form>"
    )
    body = (
        nav() + "<h1>历史对话</h1>"
        + "<a class='button' href='/quick'>＋ 新对话</a>"
        + "<section class='task-list history-list'>"
        + (conversation_section or "<p><small>还没有历史对话。</small></p>")
        + "</section>"
        + manage_bar + management_script("history-manage")
    )
    return html("历史对话", body)


@router.post("/history/manage")
@router.post("/projects/history/manage")
def history_manage(request: Request, ids: list[str] = Form(...), action: str = Form(...)):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import tasks
    import html as html_escape
    known = {item["id"] for item in tasks.conversation_roots()}
    selected = list(dict.fromkeys(item for item in ids if item in known))
    try:
        if action == "delete":
            running = []
            for task_id in selected:
                root_ids = {task_id}
                changed = True
                all_items = tasks.recent_all()
                while changed:
                    changed = False
                    for item in all_items:
                        if item.get("parent_id") in root_ids and item["id"] not in root_ids:
                            root_ids.add(item["id"])
                            changed = True
                running.extend(
                    item["id"] for item in all_items
                    if item["id"] in root_ids and item.get("status") in tasks.BLOCKING_STATUSES
                )
            if running:
                raise ValueError("选中项包含运行中的对话，请先停止。")
            for task_id in selected:
                tasks.delete_thread(task_id)
        elif action in {"pin", "unpin"}:
            tasks.update_roots(selected, action == "pin")
        else:
            raise ValueError("操作无效。")
    except ValueError as error:
        return html("无法整理对话",
            nav() + "<h1>无法整理对话</h1>"
            + "<p class='error'>" + html_escape.escape(str(error)) + "</p>"
            + "<a href='/projects/history'>返回</a>",
        )
    audit(request, "conversations_managed", action + ":" + str(len(selected)))
    return RedirectResponse("/projects/history", 303)
