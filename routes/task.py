"""Task lifecycle routes: create, view, continue, fork, approve, delete, poll."""
import html as html_escape
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from shared.config import (
    INBOX_DIR, WINDOWS_DESKTOP, WINDOWS_DESKTOP_WSL,
    kimi_default_model, kimi_default_thinking, kimi_model_display_name, kimi_models,
)
from shared.file_utils import workspace
import shared.html_lib as hlib
from shared.html_lib import (
    compact_config_controls, config_controls, full_access_pin_page,
    html, message_time, nav, status_label, upload_dialog, upload_notice,
    validate_task_config, windows_task_pin_page,
)
from shared.security import (
    audit, confirmation_signer, require_auth,
    valid_windows_pin, windows_high_risk,
)
import model_catalog

router = APIRouter()

# kimi 思考档显示名：三档与 UI 下拉一致；on 是 K2.7 的开关态（无档位）。
_KIMI_THINKING_LABELS = {"low": "Low", "high": "High", "max": "Max", "on": "开"}


# ── Task creation ──────────────────────────────────────────────────────────

@router.get("/task/new-form")
def task_form(request: Request, repeat: str = "", workdir: str = "", prompt: str = ""):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import tasks
    initial = {"target": "wsl", "workdir": str(INBOX_DIR), "prompt": ""}
    if workdir:
        initial["workdir"] = workdir
    if prompt:
        initial["prompt"] = prompt
    if repeat:
        try:
            previous, _, _ = tasks.get(repeat)
            initial = {key: previous[key] for key in ("target", "workdir", "prompt")}
        except Exception:
            pass
    target_json = json.dumps(initial["target"], ensure_ascii=True)
    workdir_json = json.dumps(initial["workdir"], ensure_ascii=True)
    prompt_val = html_escape.escape(initial["prompt"])
    repeat_note = (
        "<p><small>已载入上一次任务。你可以修改后再进入确认页，不会直接执行。</small></p>"
        if repeat else ""
    )
    form = (
        "<h1>新对话高级选项</h1>" + repeat_note
        + "<p><small>普通对话不需要设置这些选项；仅在需要 Windows 程序或指定目录时使用。</small></p>"
        + "<form action='/task/new' method='post'>"
        + "<label>执行目标</label>"
        + "<select id='target' name='target' onchange='setTarget()'>"
        + "<option value='wsl'>WSL（默认）</option>"
        + "<option value='windows'>Windows</option></select>"
        + "<label>工作目录</label>"
        + "<select id='workdir' name='workdir' onchange='setCustom()'>"
        + "<option value='" + str(INBOX_DIR) + "' data-target='wsl'>收件箱（默认）</option>"
        + "<option value='" + str(Path.home()) + "' data-target='wsl'>WSL 主目录</option>"
        + "<option value='" + WINDOWS_DESKTOP_WSL + "' data-target='wsl'>Windows 桌面（通过 WSL）</option>"
        + "<option value='/mnt/d' data-target='wsl'>D 盘（通过 WSL）</option>"
        + "<option value='" + WINDOWS_DESKTOP + "' data-target='windows'>Windows 桌面</option>"
        + "<option value='D:\\' data-target='windows'>D 盘根目录</option>"
        + "<option value='__custom__' data-target='all'>手动输入其他允许目录</option></select>"
        + "<div id='custom-wrap' class='hidden'>"
        + "<label>自定义工作目录</label>"
        + "<input id='custom-workdir' name='custom_workdir' placeholder='例如 D:\\项目'></div>"
        + "<label>任务模板</label>"
        + "<select id='template' onchange='setTemplate()'>"
        + "<option value=''>自定义任务</option>"
        + "<option value='只分析当前目录，不修改文件、不写入、不删除、不运行命令；请说明发现的内容和建议。'>只分析，不改动</option>"
        + "<option value='检查当前目录的报错或测试失败原因；不要修改文件，只给出原因和建议。'>检查报错，不改动</option></select>"
        + "<label>任务说明</label>"
        + "<textarea id='prompt' name='prompt' rows='7' required placeholder='例如：检查当前目录项目的测试失败原因，只分析，不修改文件。'>"
        + prompt_val + "</textarea>"
        + "<button>进入确认页</button></form>"
        + "<script>const initialTarget=" + target_json + ",initialWorkdir=" + workdir_json + ";"
        + "function setCustom(){const c=document.getElementById('workdir').value==='__custom__';"
        + "document.getElementById('custom-wrap').classList.toggle('hidden',!c);"
        + "document.getElementById('custom-workdir').disabled=!c;}"
        + "function setTarget(){const t=document.getElementById('target').value;"
        + "const s=document.getElementById('workdir');"
        + "for(const o of s.options){o.hidden=o.dataset.target!=='all'&&o.dataset.target!==t;}"
        + "if(s.options[s.selectedIndex].hidden){for(const o of s.options){if(!o.hidden){s.value=o.value;break;}}}setCustom();}"
        + "function setTemplate(){const v=document.getElementById('template').value;"
        + "if(v){document.getElementById('prompt').value=v;}}"
        + "document.getElementById('target').value=initialTarget;"
        + "const known=[...document.getElementById('workdir').options].some(o=>o.value===initialWorkdir);"
        + "if(known){document.getElementById('workdir').value=initialWorkdir;}"
        + "else if(initialWorkdir){document.getElementById('workdir').value='__custom__';"
        + "document.getElementById('custom-workdir').value=initialWorkdir;}setTarget();</script>"
    )
    return html("新对话高级选项", nav() + form)


@router.post("/task/new")
def task_new(request: Request, prompt: str = Form(...), workdir: str = Form(...),
             target: str = Form(...), custom_workdir: str = Form("")):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import tasks
    try:
        if target not in {"wsl", "windows"}:
            raise ValueError("执行目标无效。")
        if workdir == "__custom__":
            if not custom_workdir.strip():
                raise ValueError("请输入自定义工作目录。")
            workdir = custom_workdir.strip()
        directory = workspace(workdir, target)
    except ValueError as error:
        return html("目录无效",
            "<h1>目录无效</h1><p class='error'>" + str(error) + "</p><a href='./'>返回</a>",
        )
    if not prompt.strip() or len(prompt) > 12000:
        return html("任务无效",
            "<h1>任务无效</h1>"
            + "<p class='error'>任务说明不能为空且不超过 12000 字符。</p>"
            + "<a href='./'>返回</a>",
        )
    payload = {
        "prompt": prompt.strip(), "workdir": directory, "target": target,
        "high_risk": target == "windows" and windows_high_risk(prompt),
    }
    if payload["high_risk"]:
        token = confirmation_signer().dumps(payload)
        return html("Windows 授权",
            nav() + "<h1>需要 Windows PIN</h1>"
            + "<p class='error'>此任务可能写入、删除、安装或执行程序。</p>"
            + "<form action='/confirm' method='post'>"
            + "<input type='hidden' name='token' value='"
            + html_escape.escape(token, quote=True) + "'>"
            + "<button>继续授权</button></form>",
        )
    metadata = tasks.create(target, payload["prompt"], directory)
    tasks.start(metadata["id"])
    audit(request, "task_started", metadata["id"])
    return RedirectResponse("/task/" + metadata["id"], 303)


def _execute_confirmed(request, payload):
    """执行已授权的确认负载：建任务/激活草稿并启动，303 进对话页。

    /confirm（表单令牌）、/task/full-access 与 /task/pin（PIN 直执）共用；
    异常由调用方兜底渲染错误页。
    """
    import tasks
    if payload.get("kind") == "project":
        import projects
        project = projects.get(payload["project_id"])
        metadata = tasks.create("wsl", payload["prompt"], project["path"],
                                payload["project_id"], model=payload["model"],
                                permission="danger-full-access",
                                reasoning=payload["reasoning"],
                                agent=payload.get("agent") or None,
                                thinking=payload.get("thinking"),
                                plan_mode=bool(payload.get("plan_mode")))
        tasks.start(metadata["id"])
        projects.touch(payload["project_id"], metadata["id"], metadata["prompt"])
        audit(request, "task_started", metadata["id"])
        return RedirectResponse("/task/" + metadata["id"], 303)
    if payload.get("high_risk"):
        pin = payload.get("pin", "")
        if not pin or not valid_windows_pin(pin):
            # 尚未输 PIN（来自「继续授权」中间页）：渲染输 PIN 页；
            # PIN 不该进签名令牌（base64 可读），转发前剔除。
            if pin:
                audit(request, "windows_task_pin_failed")
            payload.pop("pin", None)
            error = "PIN 错误，请重试。" if pin else ""
            return windows_task_pin_page(confirmation_signer().dumps(payload), error)
        metadata = tasks.create(payload["target"], payload["prompt"], payload["workdir"])
        tasks.start(metadata["id"])
        audit(request, "task_started", metadata["id"])
        return RedirectResponse("/task/" + metadata["id"], 303)
    if payload.get("draft"):
        try:
            metadata = tasks.activate_draft(
                payload["draft"], payload["prompt"], payload["model"],
                "danger-full-access", payload["reasoning"],
                payload.get("agent") or None,
                thinking=payload.get("thinking"),
                plan_mode=bool(payload.get("plan_mode")),
            )
        except ValueError:
            # 重复提交（连点/重发）：草稿已被首次提交激活，直接进对话页。
            existing, _, _ = tasks.get(payload["draft"])
            if existing.get("status") != "draft":
                return RedirectResponse("/task/" + payload["draft"], 303)
            raise
        tasks.start(metadata["id"])
        audit(request, "full_access_conversation_started", metadata["id"])
        return RedirectResponse("/task/" + metadata["id"], 303)
    if payload.get("task_id"):
        previous, _, final = tasks.get(payload["task_id"])
        context = tasks.continuation_prompt(payload["task_id"], payload["prompt"])
        workdir = previous["workdir"]
        held = tasks.thread_blocking(payload["task_id"])
        metadata = tasks.create(
            previous["target"], context, workdir,
            previous.get("project_id"), previous["id"],
            model=payload["model"], permission="danger-full-access",
            reasoning=payload["reasoning"],
            user_prompt=payload["prompt"],
            agent=payload.get("agent") or None,
            held=held,
            thinking=payload.get("thinking"),
            plan_mode=bool(payload.get("plan_mode")),
        )
        if held:
            tasks.kick_held_turns()
            audit(request, "full_access_conversation_queued", metadata["id"])
        else:
            tasks.start(metadata["id"])
            audit(request, "full_access_conversation_started", metadata["id"])
        return RedirectResponse("/task/" + metadata["id"], 303)
    raise ValueError("确认内容无效。")


@router.post("/confirm")
def task_confirm(request: Request, token: str = Form(...)):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        payload = confirmation_signer().loads(token, max_age=3600)
        return _execute_confirmed(request, payload)
    except Exception as error:
        audit(request, "confirm_failed", type(error).__name__)
        return html("确认无效",
            "<h1>确认请求无效或已过期</h1>"
            + "<a href='/home'>返回首页</a>",
        )


@router.post("/task/full-access")
def task_full_access(request: Request, token: str = Form(...), pin: str = Form(...)):
    redirect = require_auth(request)
    if redirect:
        return redirect
    if not valid_windows_pin(pin):
        audit(request, "windows_bridge_pin_failed")
        return full_access_pin_page(token, "PIN 错误，请重试。")
    try:
        payload = confirmation_signer().loads(token, max_age=3600)
        return _execute_confirmed(request, payload)
    except Exception:
        return html("确认无效",
            "<h1>确认请求无效或已过期</h1>"
            + "<a href='/home'>返回首页</a>",
        )


@router.post("/task/pin")
def task_pin(request: Request, token: str = Form(...), pin: str = Form(...)):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import tasks
    try:
        payload = confirmation_signer().loads(token, max_age=3600)
        if not payload.get("high_risk"):
            raise ValueError("确认内容无效。")
    except Exception:
        return html("确认无效",
            "<h1>确认请求无效或已过期</h1>"
            + "<a href='/home'>返回首页</a>",
        )
    if not valid_windows_pin(pin):
        audit(request, "windows_task_pin_failed")
        return windows_task_pin_page(token, "PIN 错误，请重试。")
    try:
        metadata = tasks.create(payload["target"], payload["prompt"], payload["workdir"])
        tasks.start(metadata["id"])
    except Exception:
        return html("确认无效",
            "<h1>确认请求无效或已过期</h1>"
            + "<a href='/home'>返回首页</a>",
        )
    audit(request, "task_started", metadata["id"])
    return RedirectResponse("/task/" + metadata["id"], 303)


# ── Quick / draft send ─────────────────────────────────────────────────────

@router.post("/conversation/{task_id}/send")
def conversation_send(request: Request, task_id: str, prompt: str = Form(...),
                      model: str = Form(""), reasoning: str = Form("medium"),
                      permission: str = Form("workspace-write"), agent: str = Form(""),
                      thinking: str = Form(""), plan_mode: str = Form("")):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import tasks
    if not model:
        model = model_catalog.get_default_model()
    if not prompt.strip():
        return html("任务无效",
            "<h1>任务说明不能为空。</h1>"
            + "<a href='/quick'>返回</a>",
        )
    try:
        if agent and agent not in tasks.AGENT_CHOICES:
            raise ValueError("后端无效。")
        validate_task_config(model, reasoning, permission, agent)
    except ValueError as error:
        return html("配置无效",
            nav() + "<h1>配置无效</h1>"
            + "<p class='error'>" + html_escape.escape(str(error)) + "</p>"
            + "<a href='/quick'>返回</a>",
        )
    if permission == "danger-full-access":
        token = confirmation_signer().dumps({
            "draft": task_id, "prompt": prompt.strip(),
            "model": model, "reasoning": reasoning,
            "permission": permission, "agent": agent,
            "thinking": thinking or None,
            "plan_mode": bool(plan_mode),
        })
        return full_access_pin_page(token)
    try:
        metadata = tasks.activate_draft(task_id, prompt.strip(), model, permission, reasoning, agent or None,
                                        thinking=thinking or None, plan_mode=bool(plan_mode))
        tasks.start(task_id)
    except Exception:
        return html("发送失败", nav() + "<h1>发送失败</h1><a href='/quick'>重新开始</a>")
    audit(request, "task_started", task_id)
    return RedirectResponse("/task/" + task_id, 303)


# ── Continue / fork ─────────────────────────────────────────────────────────

@router.post("/task/{task_id}/continue")
def task_continue(request: Request, task_id: str, prompt: str = Form(...),
                  model: str = Form(""), reasoning: str = Form("medium"),
                  permission: str = Form("workspace-write"),
                  thinking: str = Form(""), plan_mode: str = Form("")):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import tasks
    if not model:
        model = model_catalog.get_default_model()
    if not prompt.strip() or len(prompt) > 12000:
        return html("任务无效",
            "<h1>任务说明无效</h1>"
            + "<a href='/task/" + task_id + "'>返回对话</a>",
        )
    try:
        previous, _, final = tasks.get(task_id)
        context = tasks.continuation_prompt(task_id, prompt.strip())
    except Exception:
        return html("任务不存在",
            nav() + "<h1>任务不存在</h1>"
            + "<a href='/home'>返回首页</a>",
        )
    if permission == "danger-full-access":
        token = confirmation_signer().dumps({
            "task_id": task_id, "prompt": prompt.strip(),
            "model": model, "reasoning": reasoning,
            "permission": permission,
            "agent": previous.get("agent") or "",
            "thinking": thinking or None,
            "plan_mode": bool(plan_mode),
        })
        return full_access_pin_page(token)
    held = tasks.thread_blocking(task_id)
    metadata = tasks.create(
        previous["target"], context, previous["workdir"],
        previous.get("project_id"), previous["id"],
        model=model, permission=permission,
        reasoning=reasoning, user_prompt=prompt.strip(),
        agent=previous.get("agent") or "",
        held=held,
        thinking=thinking or None,
        plan_mode=bool(plan_mode),
    )
    if held:
        # 排队自动续发：活动轮到终态后由终态钩子自动启动；
        # 这里再踢一次，兜底"判定阻塞后活动轮恰好结束"的竞态。
        tasks.kick_held_turns()
        audit(request, "task_queued", task_id + " -> " + metadata["id"])
    else:
        tasks.start(metadata["id"])
        audit(request, "task_continued", task_id + " -> " + metadata["id"])
    return RedirectResponse("/task/" + metadata["id"], 303)


@router.get("/task/{task_id}/fork")
def task_fork(request: Request, task_id: str):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import tasks
    try:
        previous, _, _ = tasks.get(task_id)
    except Exception:
        return html("任务不存在",
            nav() + "<h1>任务不存在</h1>"
            + "<a href='/home'>返回首页</a>",
        )
    agent = previous.get("agent") or "codex"
    agent_label = "Kimi" if agent == "kimi" else "Codex"
    return html("分叉对话",
        nav() + "<h1>分叉对话</h1>"
        + "<p>从现有对话分叉：<b>"
        + html_escape.escape(previous.get("title") or "未命名")
        + "</b></p>"
        + "<p>执行后端：" + agent_label
        + " · 模型：" + html_escape.escape(previous.get("model") or "")
        + " · 目录：<code>" + html_escape.escape(previous["workdir"]) + "</code></p>"
        + "<form action='/task/" + task_id + "/fork' method='post'>"
        + "<textarea name='prompt' rows='6' required autofocus "
        + "placeholder='描述你希望分叉后处理的新方向'></textarea>"
        + config_controls("task-fork-form", previous.get("model"),
                          previous.get("reasoning", "medium"),
                          previous.get("permission", "workspace-write"),
                          suffix="-fork", agent=agent,
                          thinking=previous.get("thinking"),
                          plan_mode=bool(previous.get("plan_mode", False)))
        + "<button>开始分叉任务</button></form>"
        + hlib.config_script(),
    )


@router.post("/task/{task_id}/fork")
def task_fork_create(request: Request, task_id: str, prompt: str = Form(...),
                     agent: str = Form(""), model: str = Form(""),
                     reasoning: str = Form("medium"),
                     permission: str = Form("workspace-write"),
                     thinking: str = Form(""), plan_mode: str = Form("")):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import tasks
    if not model:
        model = model_catalog.get_default_model()
    if not prompt.strip() or len(prompt) > 12000:
        return html("任务无效",
            "<h1>任务说明无效</h1>"
            + "<a href='/task/" + task_id + "/fork'>返回</a>",
        )
    try:
        previous, _, final = tasks.get(task_id)
        context = tasks.continuation_prompt(task_id, prompt.strip(), purpose="fork")
    except Exception:
        return html("任务不存在",
            nav() + "<h1>任务不存在</h1>"
            + "<a href='/home'>返回首页</a>",
        )
    if permission == "danger-full-access":
        token = confirmation_signer().dumps({
            "task_id": task_id, "prompt": prompt.strip(),
            "model": model, "reasoning": reasoning,
            "permission": permission, "agent": agent,
            "thinking": thinking or None,
            "plan_mode": bool(plan_mode),
        })
        return full_access_pin_page(token)
    metadata = tasks.create(
        previous["target"], context, previous["workdir"],
        title=prompt.strip()[:48], model=model,
        permission=permission, reasoning=reasoning,
        user_prompt=prompt.strip(), agent=agent,
        thinking=thinking or None,
        plan_mode=bool(plan_mode),
    )
    tasks.start(metadata["id"])
    audit(request, "task_forked", task_id + " -> " + metadata["id"])
    return RedirectResponse("/task/" + metadata["id"], 303)


# ── Stop / cancel ───────────────────────────────────────────────────────────

@router.post("/task/{task_id}/cancel")
def task_cancel(request: Request, task_id: str):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import tasks
    try:
        tasks.cancel(task_id)
    except Exception:
        pass
    return RedirectResponse("/task/" + task_id, 303)


# ── Windows approval ────────────────────────────────────────────────────────

@router.post("/task/{task_id}/windows/approve")
def windows_request_approve(request: Request, task_id: str,
                            request_id: str = Form(...), pin: str = Form(...)):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import tasks
    if not valid_windows_pin(pin):
        audit(request, "windows_bridge_pin_failed")
        return RedirectResponse("/task/" + task_id + "?pin_error=1", 303)
    try:
        tasks.approve_windows(task_id, request_id)
    except ValueError:
        # 重复提交（连点/重发）：首次批准已生效或请求已处理，直接回对话页。
        return RedirectResponse("/task/" + task_id, 303)
    except Exception as error:
        return html("授权失败",
            "<h1>授权失败</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p>"
            + "<a href='/task/" + task_id + "'>返回对话</a>",
        )
    return RedirectResponse("/task/" + task_id, 303)


@router.post("/task/{task_id}/windows/reject")
def windows_request_reject(request: Request, task_id: str, request_id: str = Form(...)):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import tasks
    try:
        tasks.reject_windows(task_id, request_id)
    except Exception as error:
        return html("拒绝失败",
            "<h1>拒绝失败</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p>"
            + "<a href='/task/" + task_id + "'>返回对话</a>",
        )
    return RedirectResponse("/task/" + task_id, 303)


# ── Delete conversation ─────────────────────────────────────────────────────

@router.get("/task/{task_id}/delete")
def task_delete_confirm(request: Request, task_id: str):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import tasks
    try:
        metadata, _, _ = tasks.get(task_id)
    except Exception:
        return html("任务不存在",
            nav() + "<h1>任务不存在</h1>"
            + "<a href='/home'>返回首页</a>",
        )
    if metadata.get("status") in tasks.BLOCKING_STATUSES:
        return html("无法删除",
            nav() + "<h1>无法删除</h1>"
            + "<p class='error'>任务仍在运行，请先停止。</p>"
            + "<a href='/task/" + task_id + "'>返回</a>",
        )
    return html("确认删除对话",
        nav() + "<h1>确认删除对话</h1>"
        + "<p class='error'>删除整个对话链（含继续和分叉），不可恢复。</p>"
        + "<p><b>" + html_escape.escape(metadata.get("title") or "未命名") + "</b></p>"
        + "<form action='/task/" + task_id + "/delete' method='post'>"
        + "<button>确认删除</button></form>"
        + "<p><a href='/task/" + task_id + "'>返回</a></p>",
    )


@router.post("/task/{task_id}/delete")
def task_delete(request: Request, task_id: str):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import tasks
    try:
        root_ids = {task_id}
        changed = True
        all_items = tasks.recent_all()
        while changed:
            changed = False
            for item in all_items:
                if item.get("parent_id") in root_ids and item["id"] not in root_ids:
                    root_ids.add(item["id"])
                    changed = True
        running = [item["id"] for item in all_items
                   if item["id"] in root_ids and item.get("status") in tasks.BLOCKING_STATUSES]
        if running:
            raise ValueError("对话链中有运行中的任务。")
        tasks.delete_thread(task_id)
        audit(request, "conversation_deleted", task_id)
        return RedirectResponse("/home", 303)
    except Exception as error:
        return html("删除失败",
            "<h1>删除失败</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p>"
            + "<a href='/task/" + task_id + "'>返回</a>",
        )


# ── Convert to project ──────────────────────────────────────────────────────

@router.get("/task/{task_id}/to-project")
def task_to_project_form(request: Request, task_id: str):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import tasks
    try:
        tasks.get(task_id)
    except Exception:
        return html("任务不存在",
            nav() + "<h1>任务不存在</h1>"
            + "<a href='/home'>返回首页</a>",
        )
    return html("转为项目",
        nav() + "<h1>转为项目</h1>"
        + "<p>将当前对话及其附件移动到新项目，方便集中管理。</p>"
        + "<form action='/task/" + task_id + "/to-project' method='post'>"
        + "<label>项目名称</label><input name='name' required>"
        + "<label><input type='checkbox' name='move_attachments' value='1'>"
        + " 移动附件到项目目录</label>"
        + "<button>创建项目</button></form>",
    )


@router.post("/task/{task_id}/to-project")
def task_to_project(request: Request, task_id: str, name: str = Form(...),
                    move_attachments: str = Form("")):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import tasks, projects, shutil
    try:
        metadata, _, _ = tasks.get(task_id)
    except Exception:
        return html("任务不存在",
            nav() + "<h1>任务不存在</h1>"
            + "<a href='/home'>返回首页</a>",
        )
    try:
        project = projects.create(name)
        projects.touch(project["id"], task_id, metadata.get("prompt", ""))
        if move_attachments == "1":
            from pathlib import Path
            src = INBOX_DIR / "conversations" / task_id
            if src.exists():
                upload_path = Path(project["path"]) / "手机上传资料"
                upload_path.mkdir(mode=0o700, exist_ok=True)
                for f in src.iterdir():
                    if f.is_file():
                        shutil.copy2(f, upload_path / f.name)
        audit(request, "task_to_project", task_id + " -> " + project["id"])
        return RedirectResponse("/project/" + quote(project["id"]), 303)
    except ValueError as error:
        return html("创建项目失败",
            "<h1>创建项目失败</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p>"
            + "<a href='/task/" + task_id + "/to-project'>返回</a>",
        )

# ── Task view (biggest route) ───────────────────────────────────────────────

@router.get("/task/{task_id}")
def task_view(request: Request, task_id: str, continue_form: int = 0, uploaded: str = "", pin_error: str = ""):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import tasks, projects
    try:
        metadata, output, final = tasks.get(task_id)
    except Exception:
        return html("任务不存在",
            nav() + "<h1>任务不存在</h1>"
            + "<a href='/home'>返回首页</a>",
        )
    if metadata.get("windows_followup_id"):
        return RedirectResponse("/task/" + metadata["windows_followup_id"], 303)
    summary = tasks.summarize(output)
    actual = tasks.actual_config(output, metadata["target"])
    safe_output = html_escape.escape(output)
    safe_final = html_escape.escape(final)
    safe_summary = html_escape.escape(summary)
    status = metadata["status"]
    target = "Windows" if metadata["target"] == "windows" else "WSL"
    safe_workdir = html_escape.escape(metadata["workdir"])
    requested_model = metadata.get("model") or model_catalog.get_default_model()
    requested_reasoning = metadata.get("reasoning", "medium")
    actual_model = actual.get("model") if actual else None
    actual_reasoning = actual.get("reasoning") if actual else None
    actual_model_text = html_escape.escape(actual_model or requested_model)
    actual_reasoning_text = html_escape.escape(actual_reasoning or requested_reasoning)
    if metadata["target"] == "windows" and actual and not actual_model:
        actual_model_text = actual_reasoning_text = "Windows 会话信息尚未回传"
    agent_value = metadata.get("agent") or "codex"
    agent_label = "Kimi" if agent_value == "kimi" else "Codex"
    if agent_value == "kimi":
        # 模型与思考等级都显示实际生效值（2026-07-29 用户重申的要求）：
        # 模型与 start() 同一兜底链；思考等级优先轮末收割的 wire 实际值，
        # 其次任务显式选择，未运行的任务显示 start() 会发的工作台默认。
        kimi_model_value = (metadata.get("actual_model") or metadata.get("model")
                            or kimi_default_model())
        actual_model_text = html_escape.escape(kimi_model_display_name(kimi_model_value))
        effective_thinking = (metadata.get("actual_thinking") or metadata.get("thinking")
                              or kimi_default_thinking())
        actual_reasoning_text = html_escape.escape(
            _KIMI_THINKING_LABELS.get(effective_thinking, effective_thinking))
        mismatch = False
    else:
        mismatch = bool(
            (actual_model and actual_model != requested_model)
            or (actual_reasoning and actual_reasoning != requested_reasoning)
        )
    active = status in tasks.BLOCKING_STATUSES
    elapsed = int(metadata.get("elapsed_seconds", 0))
    no_output = int(metadata.get("no_output_seconds", 0))
    runtime_text = ("运行 " + str(elapsed // 60) + ":" + str(elapsed % 60).zfill(2) if active else "耗时 " + str(elapsed // 60) + ":" + str(elapsed % 60).zfill(2))
    warning_text = (
        "暂时没有新的执行输出，可能正在等待上游模型；可以继续等待或停止任务。"
        if metadata.get("output_warning") and active else ""
    )
    # 懒恢复判定：succeeded 但任务记录未经正常收割（无 returncode）。
    recovered = status == "succeeded" and (
        bool(metadata.get("recovered")) or "returncode" not in metadata
    )

    # Summary section
    summary_section = (
        "<h2>执行摘要</h2><pre id='live-summary'>"
        + (safe_summary or "暂无摘要。") + "</pre>"
    )

    # Stop form：挪到对话链构建之后渲染（见下方 active_turn 段），
    # 排队语义下停止按钮要指向同对话的实际活动轮。

    # Build message chain
    messages = []
    try:
        chain = tasks.thread(task_id)
    except Exception:
        chain = [metadata]
    for turn_index, turn in enumerate(chain):
        turn_metadata, turn_output, turn_final = tasks.get(turn["id"])
        staged_msgs = tasks.task_agent_messages(turn["id"])
        staged_reply = staged_msgs[-1] if staged_msgs else ""
        display_prompt = tasks.user_prompt(turn)
        user_meta = (message_time(turn.get("created"))
                     + " · " + str(len(display_prompt)) + " 字")
        # 排队挂起轮在消息上直接挂「等待中」徽章：顶部小徽章不显眼，
        # 用户需要一眼看到是哪条消息在排队（前轮终态后自动开跑）。
        queued_badge = (
            " <span class='status queued' title='前面的任务完成后自动开始'>等待中</span>"
            if turn.get("held") and turn.get("status") == "queued" else ""
        )
        auto_collapse = turn_index < len(chain) - 2 and len(display_prompt) > 900
        body_class = "message-body collapsed" if auto_collapse else "message-body"
        collapsed_summary = (
            "<div class='collapsed-summary'>"
            + "<span>较早长消息 · " + str(len(display_prompt)) + " 字</span>"
            + "<button type='button' class='secondary' data-expand>展开</button></div>"
        )
        messages.append(
            "<div class='message-wrap user'>"
            + "<article class='bubble user' data-message data-message-id='"
            + turn["id"] + "-user'>"
            + "<button class='message-menu-button' type='button' aria-label='消息操作' title='消息操作' data-menu>⋯</button>"
            + "<div class='message-menu hidden'>"
            + "<button type='button' class='secondary' data-copy>复制全文</button>"
            + "<button type='button' class='secondary' data-quote>引用到输入框</button>"
            + "<button type='button' class='secondary' data-important>标记重要</button>"
            + "<button type='button' class='secondary' data-toggle>"
            + ("展开" if auto_collapse else "折叠") + "</button></div>"
            + "<div class='" + body_class + "'>" + html_escape.escape(display_prompt) + "</div>"
            + collapsed_summary + "</article>"
            + "<div class='message-meta'>" + html_escape.escape(user_meta) + queued_badge + "</div></div>"
        )
        if turn_final:
            agent_meta = (
                message_time(turn.get("finished") or turn.get("last_output_at")
                            or turn.get("created"))
                + " · " + str(len(turn_final)) + " 字"
            )
            auto_collapse = turn_index < len(chain) - 2 and len(turn_final) > 1200
            body_class = "message-body collapsed" if auto_collapse else "message-body"
            collapsed_summary = (
                "<div class='collapsed-summary'>"
                + "<span>较早长消息 · " + str(len(turn_final)) + " 字</span>"
                + "<button type='button' class='secondary' data-expand>展开</button></div>"
            )
            messages.append(
                "<div class='message-wrap agent'>"
                + "<article class='bubble agent' data-message data-message-id='"
                + turn["id"] + "-agent'>"
                + "<button class='message-menu-button' type='button' aria-label='消息操作' title='消息操作' data-menu>⋯</button>"
                + "<div class='message-menu hidden'>"
                + "<button type='button' class='secondary' data-copy>复制全文</button>"
                + "<button type='button' class='secondary' data-quote>引用到输入框</button>"
                + "<button type='button' class='secondary' data-important>标记重要</button>"
                + "<button type='button' class='secondary' data-toggle>"
                + ("展开" if auto_collapse else "折叠") + "</button></div>"
                + "<div class='" + body_class + "'>" + html_escape.escape(turn_final) + "</div>"
                + collapsed_summary + "</article>"
                + "<div class='message-meta'>" + html_escape.escape(agent_meta) + "</div></div>"
            )
        elif staged_reply:
            stage_state = (
                "任务处理中" if turn_metadata.get("status") in {"queued", "running", "stopping"}
                else "任务未完成"
            )
            agent_meta = (
                message_time(turn_metadata.get("last_output_at") or turn_metadata.get("created"))
                + " · 阶段性回复 · " + stage_state
            )
            messages.append(
                "<div class='message-wrap agent'>"
                + "<article class='bubble agent staged' data-message data-message-id='"
                + turn["id"] + "-staged'>"
                + "<button class='message-menu-button' type='button' aria-label='消息操作' title='消息操作' data-menu>⋯</button>"
                + "<div class='message-menu hidden'>"
                + "<button type='button' class='secondary' data-copy>复制全文</button>"
                + "<button type='button' class='secondary' data-quote>引用到输入框</button>"
                + "<button type='button' class='secondary' data-important>标记重要</button>"
                + "<button type='button' class='secondary' data-toggle>折叠</button></div>"
                + "<div class='message-body'>" + html_escape.escape(staged_reply) + "</div>"
                + "<div class='collapsed-summary'><span>阶段性回复</span>"
                + "<button type='button' class='secondary' data-expand>展开</button></div></article>"
                + "<div class='message-meta'>" + html_escape.escape(agent_meta) + "</div></div>"
            )

    # 排队语义：停止按钮指向同对话的实际活动轮（本页轮可能是挂起轮）。
    active_turn = next(
        (turn for turn in reversed(chain)
         if turn.get("status") in {"queued", "running"} and not turn.get("held")),
        None,
    )
    stop_form = ""
    if active_turn:
        stop_form = (
            "<form id='thread-stop' action='/task/" + active_turn["id"] + "/cancel' method='post' "
            + "onsubmit=\"return confirm('确认停止当前任务？已收到的阶段性回复会保留，但可能无法形成最终答复；已经发生的文件改动不会自动撤销。')\"></form>"
        )

    # Composer
    attachment_directory = (
        projects.upload_directory(metadata["project_id"])
        if metadata.get("project_id") else metadata["workdir"]
    )
    attachment = (
        upload_dialog(attachment_directory, "/task/" + task_id, "thread-upload")
        if metadata["target"] == "wsl" else ""
    )
    compact_config = compact_config_controls(
        "thread-send", metadata.get("model") or model_catalog.get_default_model(),
        metadata.get("reasoning", "medium"),
        metadata.get("permission", "workspace-write"),
        "-thread", agent=metadata.get("agent"), agent_locked=True,
        thinking=metadata.get("thinking"),
        plan_mode=bool(metadata.get("plan_mode", False)),
    )
    composer_action = (
        # 活动轮存在：停止+发送并存（发送=排队自动续发，活动轮终态后自动接上）。
        "<button class='stop-button' form='thread-stop' title='停止当前任务'>停止</button>"
        + "<button class='send-button' form='thread-send'>发送</button>"
        if active_turn else
        "<button class='stop-button' type='button' title='滚动到页面顶部的授权卡片' "
        + "onclick=\"document.getElementById('windows-approval')?.scrollIntoView({behavior:'smooth',block:'center'})\">前往授权</button>"
        if status == "waiting_approval" else
        "<button class='stop-button' type='button' disabled>"
        + ("执行中" if status == "windows_executing" else "停止中") + "</button>"
        if status in {"stopping", "windows_executing"} else
        "<button class='send-button' form='thread-send'>发送</button>"
    )
    composer = (
        "<div class='chat-composer bottom-composer' id='composer'>"
        + stop_form
        + "<form id='thread-send' action='/task/" + task_id + "/continue' method='post'></form>"
        + "<textarea form='thread-send' name='prompt' rows='3' required placeholder='"
        + ("可先输入下一条消息…" if active else "继续输入…")
        + "' data-draft-key='codex-thread-draft-" + task_id + "'></textarea>"
        + "<div class='composer-compact-row'>"
        + compact_config + attachment
        + "<button class='refresh-button icon-button' type='button' onclick='location.reload()' title='刷新对话'>↻</button>"
        + composer_action + "</div></div>"
    )

    # Navigation & info
    back = (
        "/project/" + quote(metadata["project_id"])
        if metadata.get("project_id") else "/home"
    )
    convert = (
        "<a class='button secondary' href='/task/" + task_id + "/to-project'>转为项目</a>"
        if not metadata.get("project_id") and not active else ""
    )
    fork = (
        "<a class='button secondary' href='/task/" + task_id + "/fork'>分叉</a>"
        if not active else ""
    )
    delete_conversation = (
        "<a class='button danger' href='/task/" + task_id + "/delete'>删除对话</a>"
        if metadata.get("project_id") else ""
    )
    warning = (
        "<p class='error'>实际运行配置与请求配置不同，请以实际运行值为准。</p>"
        if mismatch else ""
    )
    context_line = ""
    if metadata.get("context_used"):
        used = int(metadata["context_used"])
        used_text = f"{used / 1024:.1f}K"
        limit = metadata.get("context_limit")
        if limit:
            limit_text = f"{int(limit) / 1024:.1f}K"
            pct = used / int(limit) * 100
            context_line = (
                f"<p>上下文：<code>{used_text} / {limit_text}（{pct:.0f}%）</code></p>"
            )
        else:
            context_line = f"<p>上下文：<code>{used_text} / ?</code></p>"
    info = (
        "<p>执行后端：<code>" + agent_label + "</code></p>"
        + "<p>模型：<code>" + actual_model_text + "</code></p>"
        + "<p>思考等级：<code>" + actual_reasoning_text + "</code></p>"
        + context_line
        + "<p>执行权限：<code>"
        + html_escape.escape(metadata.get("permission", "workspace-write"))
        + "</code></p><p>目录：<code>" + safe_workdir + "</code></p>"
        + warning
    )
    # ── two-row header body ─────────────────────────────────────────────────
    runtime_compact = (
        "<span class='runtime-compact'>"
        + actual_model_text + " · " + actual_reasoning_text + "</span>"
    )

    # Line 1: status badge + title
    body_line1 = (
        "<div class='conversation-head-line'>"
        + "<div class='conversation-title'><span id='live-status-label' class='status "
        + html_escape.escape(status) + (" recovered" if recovered else "") + "'>"
        + html_escape.escape(status_label(status)) + "</span></div>"
        + "<span class='head-title'>"
        + html_escape.escape(metadata.get("title") or "未命名") + "</span>"
        + "</div>"
    )

    # Line 2: runtime info (varies by state)
    body_line2 = ""
    if active:
        body_line2 = (
            "<div class='conversation-head-line'>"
            + "<span id='live-runtime'>" + html_escape.escape(runtime_text) + "</span>"
            + "<span id='live-output-state'>"
            + (" · 最后 " + str(no_output // 60) + ":" + str(no_output % 60).zfill(2) if active else "")
            + "</span><span id='live-warning' class='error'>"
            + html_escape.escape(warning_text) + "</span></div>"
        )
    elif recovered:
        body_line2 = (
            "<div class='conversation-head-line'>"
            + "<span class='notice-recovered'>已"
            "完成（中断后恢复），输出可能不完整</span></div>"
        )

    # Body div (chat-live.js looks for #live-status with data-task-id / data-active)
    body_div = (
        "<div id='live-status' class='conversation-head-body' data-task-id='" + task_id
        + "' data-active='" + ("1" if active else "0") + "'>"
        + body_line1 + body_line2 + "</div>"
    )

    tool_section = tasks.render_tool_groups(task_id, active=active)
    execution_details = (
        "<details id='execution-details'><summary>执行详情</summary>"
        + summary_section
        + tool_section
        + "<pre id='live-output'>" + safe_output + "</pre>"
        + "<p><small>停止只能阻止后续操作，已经发生的文件改动不会自动撤销。</small></p></details>"
    )

    # Approval panel
    approval_panel = ""
    if status in {"waiting_approval", "windows_executing", "windows_execution_uncertain"}:
        req_detail = tasks.approval_detail(task_id) or {}
        req_id = html_escape.escape(str(req_detail.get("id", "")), quote=True)
        req_source = req_detail.get("source", "windows")
        source_label = "本地操作" if req_source == "local" else "Windows 操作"
        req_summary = html_escape.escape(str(req_detail.get("summary") or source_label))
        req_reason = html_escape.escape(str(req_detail.get("reason") or ""))
        req_action = html_escape.escape(str(req_detail.get("action", "")))
        if status == "waiting_approval":
            approval_panel = (
                "<div id='windows-approval' class='windows-approval'>"
                + "<h2>" + source_label + "待审批</h2>"
                + "<p><b>操作：</b>" + req_action + "</p>"
                + "<p>" + req_summary + "</p>"
                + ("<p><small>" + req_reason + "</small></p>" if req_reason else "")
                + ("<p class='error'>PIN 错误，请重试。</p>" if pin_error else "")
                + "<form action='/task/" + task_id + "/windows/approve' method='post' style='display:inline' onsubmit=\"var b=this.querySelector('button');if(b.disabled)return false;b.disabled=true;b.textContent='执行中，请稍候…';return true\">"
                + "<input type='hidden' name='request_id' value='" + req_id + "'>"
                + "<input name='pin' type='password' inputmode='numeric' placeholder='PIN' required>"
                + "<button>批准</button></form>"
                + "<form action='/task/" + task_id + "/windows/reject' method='post' style='display:inline'>"
                + "<input type='hidden' name='request_id' value='" + req_id + "'>"
                + "<button class='danger'>拒绝</button></form></div>"
            )
        else:
            req_status = {
                "windows_executing": "执行中", "windows_execution_uncertain": "等待确认",
            }.get(status, "处理中")
            approval_panel = (
                "<div id='windows-approval' class='windows-approval'>"
                + "<h2>" + source_label + " — " + req_status + "</h2>"
                + "<p><b>操作：</b>" + req_action + "</p>"
                + "<p>" + req_summary + "</p></div>"
            )

    head = (
        "<header class='conversation-head'><div class='conversation-head-row'>"
        + "<a class='button secondary icon-button' href='" + back
        + "' title='返回' aria-label='返回'>‹</a>"
        + body_div
        + "<details class='head-menu'><summary title='对话信息与更多操作' "
        + "aria-label='对话信息与更多操作'>⋯</summary><div class='head-menu-panel'>"
        + info + execution_details
        + "<div class='toolbar'>" + convert + fork + delete_conversation + "</div>"
        + "</div></details></div>"
        # 审批卡移入吸顶头部：等待审批/执行中期间恒贴页面顶部，不随滚动消失
        + approval_panel + "</header>"
    )

    # JS bundles (inlined — same as v1)
    behavior = '<script src="/static/behavior.js"></script>'
    draft_behavior = '<script src="/static/draft.js"></script>'
    scroll_tools = (
        "<div class='scroll-tools' data-scroll-tools data-task-id='" + task_id + "'>"
        + "<button class='new-reply-button hidden' type='button' data-new-reply>有新回复</button>"
        + "<button class='jump-bottom-button hidden' type='button' title='回到底部' "
        + "aria-label='回到底部' data-jump-bottom>↓</button></div>"
    )
    scroll_behavior = '<script src="/static/scroll.js"></script>'
    live_behavior = (
        "<script src='" + hlib.static_url("/static/chat-live.js") + "'></script>"
        "<script src='" + hlib.static_url("/static/js/ws-client.js") + "'></script>"
    )


    return html("对话",
        nav() + head + upload_notice(uploaded)
        + "<section class='conversation-messages'>" + "".join(messages) + "</section>"
        + composer + scroll_tools + live_behavior,
    )


# ── Task status poll ────────────────────────────────────────────────────────

@router.get("/task/{task_id}/status")
def task_status(request: Request, task_id: str, details: int = 0):
    redirect = require_auth(request)
    if redirect:
        return redirect
    import tasks
    try:
        snapshot = tasks.status_snapshot(task_id, include_details=details == 1)
    except Exception:
        return JSONResponse({"error": "任务不存在"}, status_code=404)
    snapshot["label"] = status_label(snapshot["status"])
    return JSONResponse(snapshot)
