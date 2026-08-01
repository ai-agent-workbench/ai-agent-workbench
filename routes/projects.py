"""Project management routes."""
import html as html_escape
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from shared.config import FILE_ROOTS, SENSITIVE_PATHS
from shared.file_utils import workspace
import shared.html_lib as hlib
from shared.html_lib import (
    config_script, _conversation_groups, compact_config_controls, config_controls,
    full_access_pin_page, html, management_script, nav, upload_dialog, upload_notice,
    validate_task_config,
)
from shared.security import audit, confirmation_signer, require_auth

router = APIRouter()


@router.get("/project/new")
def project_new_form(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    body = (
        nav() + "<h1>新建项目</h1>"
        + "<form action='/project/new' method='post'>"
        + "<label>项目名称</label><input name='name' required placeholder='例如：Web 工作台改进'>"
        + "<label>模板</label>"
        + "<select name='template'>"
        + "<option value='blank'>空白项目</option>"
        + "<option value='document'>文档项目</option>"
        + "<option value='organize'>整理项目</option>"
        + "</select>"
        + "<button>创建项目</button></form>"
    )
    return html("新建项目", body)


@router.get("/projects")
def project_list(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import projects
    cards = []
    for project in projects.recent():
        cards.append(
            "<label class='manage-card' data-id='" + project["id"] + "'>"
            "<input class='manage-check' type='checkbox' form='projects-manage' "
            "name='ids' value='" + project["id"] + "'>"
            "<a class='card' href='/project/" + quote(project["id"]) + "'>"
            "<b>" + html_escape.escape(project["name"]) + "</b></a></label>"
        )
    manage_bar = (
        "<form id='projects-manage' class='manage-bar' action='/projects/manage' method='post'>"
        "<button type='button' class='secondary' data-all>全选</button>"
        "<button name='action' value='pin'>置顶</button>"
        "<button name='action' value='unpin' class='secondary'>取消置顶</button>"
        "<button name='action' value='delete' class='danger'>删除</button>"
        "<button type='button' class='secondary' data-cancel><span data-count>0 项</span> 取消</button>"
        "</form>"
    )
    body = (
        nav() + "<h1>项目</h1>"
        + "<a class='button' href='/project/new'>＋ 新建项目</a>"
        + "<section class='cards'>"
        + ("".join(cards) or "<p><small>还没有项目。</small></p>")
        + "</section>" + manage_bar + management_script("projects-manage")
    )
    return html("项目", body)


@router.post("/projects/manage")
def projects_manage(request: Request, ids: list[str] = Form(...), action: str = Form(...)):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import tasks, projects
    try:
        all_projects = {p["id"] for p in projects.recent()}
    except Exception:
        return html("项目不存在", nav() + "<h1>项目不存在</h1><a href='/home'>返回首页</a>")
    selected = list(dict.fromkeys(item for item in ids if item in all_projects))
    try:
        if action == "delete":
            selected_related = {pid: tasks.for_project(pid) for pid in selected}
            if any(
                item.get("status") in tasks.BLOCKING_STATUSES
                for related in selected_related.values() for item in related
            ):
                raise ValueError("选中项目包含运行中的对话，请先停止。")
            for project_id in selected:
                projects.delete(project_id)
        elif action in {"pin", "unpin"}:
            projects.update_order(selected, action == "pin")
        else:
            raise ValueError("操作无效。")
    except ValueError as error:
        return html("无法整理项目",
            nav() + "<h1>无法整理项目</h1>"
            + "<p class='error'>" + html_escape.escape(str(error)) + "</p>"
            + "<a href='/projects'>返回</a>",
        )
    audit(request, "projects_managed", action + ":" + str(len(selected)))
    return RedirectResponse("/projects", 303)


@router.post("/project/new")
def project_new(request: Request, name: str = Form(...), template: str = Form("blank")):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import projects
    try:
        project = projects.create(name, template)
        audit(request, "project_created", project["id"])
        return RedirectResponse("/project/" + quote(project["id"]), 303)
    except ValueError as error:
        return html("创建项目失败",
            "<h1>创建项目失败</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='/project/new'>返回</a>",
        )


@router.get("/project/{project_id}")
def project_view(request: Request, project_id: str, uploaded: str = ""):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import tasks, projects
    try:
        project = projects.get(project_id)
    except Exception:
        return html("项目不存在", nav() + "<h1>项目不存在</h1><a href='/home'>返回首页</a>")
    all_related = tasks.for_project(project_id)
    by_id = {item["id"]: item for item in all_related}
    root_items = [item for item in all_related if not item.get("parent_id")]
    for item in root_items:
        latest_id = tasks.latest_in_thread(item["id"], items=all_related)
        item["latest_id"] = latest_id
        latest = by_id.get(latest_id, item)
        item["latest_status"] = latest.get("status", item.get("status"))
        item["latest_activity"] = latest.get("finished") or latest.get("started") or latest.get("created")
    root_items.sort(key=lambda i: i.get("latest_activity", i["created"]), reverse=True)
    from shared.html_lib import _conversation_groups
    task_items = _conversation_groups(root_items, "project-conversation-manage", show_project=False)
    upload_path = projects.upload_directory(project_id)
    project_url = "/project/" + quote(project_id)
    manage_bar = (
        "<form id='project-conversation-manage' class='manage-bar' "
        "action='/project/" + quote(project_id) + "/manage' method='post'>"
        "<button type='button' class='secondary' data-all>全选</button>"
        "<button name='action' value='pin'>置顶</button>"
        "<button name='action' value='unpin' class='secondary'>取消置顶</button>"
        "<button name='action' value='up' class='secondary'>上移</button>"
        "<button name='action' value='down' class='secondary'>下移</button>"
        "<button name='action' value='delete' class='danger'>删除</button>"
        "<button type='button' class='secondary' data-cancel><span data-count>0 项</span> 取消</button>"
        "</form>"
    )
    body = (
        nav() + "<h1>" + html_escape.escape(project["name"]) + "</h1>"
        + upload_notice(uploaded)
        + "<div class='toolbar'>"
        + "<a class='button secondary' href='/files?path=" + quote(project["path"]) + "'>项目文件</a>"
        + "<a class='button secondary' href='/edit?path=" + quote(project["path"] + "/进度与待办.md") + "'>进度与待办</a>"
        + "<a class='button secondary' href='/takeover?project_id=" + quote(project_id) + "'>接管历史会话</a></div>"
        + "<div class='chat-composer' id='new-conversation'><h2>新建项目对话</h2>"
        + "<form id='project-send' action='/project/task' method='post'>"
        + "<input type='hidden' name='project_id' value='" + html_escape.escape(project_id, quote=True) + "'>"
        + "</form>"
        + "<textarea form='project-send' name='prompt' rows='5' required placeholder='这条对话准备处理什么？'></textarea>"
        + "<div class='composer-actions'>"
        + upload_dialog(upload_path, project_url, "project-upload")
        + "<button class='send-button' form='project-send'>开始</button></div>"
        + config_controls("project-send", suffix="-project")
        + "</div>"
        + "<h2>项目对话</h2>"
        + "<section class='task-list history-list'>"
        + (task_items or "<p><small>还没有对话。</small></p>")
        + "</section>"
        + manage_bar + management_script("project-conversation-manage")
        + config_script()
    )
    return html(project["name"], body)


@router.post("/project/task")
def project_task(request: Request, project_id: str = Form(...), prompt: str = Form(...),
                 model: str = Form(""), reasoning: str = Form("medium"),
                 permission: str = Form("workspace-write"), agent: str = Form("")):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import tasks, projects
    import model_catalog
    if not model:
        model = model_catalog.get_default_model()
    try:
        project = projects.get(project_id)
        directory = workspace(project["path"], "wsl")
    except Exception:
        return html("项目无效", nav() + "<h1>项目无效</h1><a href='/home'>返回首页</a>")
    if not prompt.strip() or len(prompt) > 12000:
        return html("任务无效",
            nav() + "<h1>任务说明无效</h1>"
            + "<a href='/project/" + quote(project_id) + "'>返回项目</a>",
        )
    try:
        if agent and agent not in tasks.AGENT_CHOICES:
            raise ValueError("后端无效。")
        validate_task_config(model, reasoning, permission, agent)
    except ValueError as error:
        return html("配置无效",
            nav() + "<h1>配置无效</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p>"
            + "<a href='/project/" + quote(project_id) + "'>返回</a>",
        )
    if permission == "danger-full-access":
        token = confirmation_signer().dumps({
            "kind": "project", "project_id": project_id, "prompt": prompt.strip(),
            "model": model, "reasoning": reasoning, "agent": agent,
        })
        return full_access_pin_page(token)
    metadata = tasks.create("wsl", prompt.strip(), directory, project_id,
                            model=model, permission=permission,
                            reasoning=reasoning, agent=agent or None)
    tasks.start(metadata["id"])
    projects.touch(project_id, metadata["id"], metadata["prompt"])
    audit(request, "task_started", metadata["id"])
    return RedirectResponse("/task/" + metadata["id"], 303)


@router.post("/project/{project_id}/manage")
def project_conversation_manage(request: Request, project_id: str,
                                ids: list[str] = Form(...), action: str = Form(...)):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import tasks, projects
    try:
        projects.get(project_id)
    except Exception:
        return html("项目不存在", nav() + "<h1>项目不存在</h1><a href='/home'>返回首页</a>")
    known = {item["id"] for item in tasks.for_project(project_id) if not item.get("parent_id")}
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
        elif action in {"up", "down"}:
            ordered = [item["id"] for item in sorted(
                tasks.for_project(project_id),
                key=lambda i: (not i.get("pinned", False), i.get("sort_order", 0))
            ) if not item.get("parent_id")]
            iterator = range(1, len(ordered)) if action == "up" else range(len(ordered) - 2, -1, -1)
            for index in iterator:
                other = index - 1 if action == "up" else index + 1
                if ordered[index] in selected and ordered[other] not in selected:
                    ordered[index], ordered[other] = ordered[other], ordered[index]
            tasks.update_roots(ordered)
        else:
            raise ValueError("操作无效。")
    except ValueError as error:
        return html("无法整理对话",
            nav() + "<h1>无法整理对话</h1>"
            + "<p class='error'>" + html_escape.escape(str(error)) + "</p>"
            + "<a href='/project/" + quote(project_id) + "'>返回</a>",
        )
    audit(request, "project_conversations_managed", action + ":" + str(len(selected)))
    return RedirectResponse("/project/" + quote(project_id), 303)
