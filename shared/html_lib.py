"""HTML builder helpers used across route modules.

Every `def` here that was previously a top-level function in app.py
keeps the identical signature and behaviour.  The Jinja2 `templates`
object lives here so that `html()` can render base.html without every
route needing its own instance.
"""
import json
import html as html_escape
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from shared.config import (
    ALL_REASONING_LEVELS, INBOX_DIR, PERMISSIONS, STATUS_LABELS,
    WINDOWS_DESKTOP, WINDOWS_DESKTOP_WSL,
    kimi_default_model, kimi_model_display_name, kimi_models,
)


# ── Jinja2 instance (single shared instance) ────────────────────────────────

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def static_url(path):
    """给 /static 资源 URL 追加 ?v=<mtime> 缓存穿透版本号。

    页面 HTML 每次实时渲染不缓存，文件一变版本号即变，浏览器必然重新下载。
    根治手机端启发式缓存致已发布 JS 长期不生效（2026-07-28 实证：无缓存头
    时期入缓存的旧条目按文件年龄 10% 视为新鲜，数小时不回源，no-cache 管不到）。
    """
    try:
        mtime = int((STATIC_DIR / path[len("/static/"):]).stat().st_mtime)
    except OSError:
        mtime = 0
    return path + "?v=" + str(mtime)


templates.env.globals["static_url"] = static_url


# ── config_script (JS for dynamic model/reasoning/agent selects) ────────────

def config_script():
    return "<script src='" + static_url("/static/config.js") + "'></script>"

# ── Page-level builders ─────────────────────────────────────────────────────

def html(title, body):
    """Render a page using the Jinja2 base template (previously in app.py)."""
    return HTMLResponse(templates.get_template("base.html").render(
        title=title, content=body,
    ))


def nav():
    return (
        "<nav class='nav'>"
        "<a href='/home'><span>⌂</span>首页</a>"
        "<a href='/projects'><span>▤</span>项目</a>"
        "<a href='/projects/history'><span>◷</span>历史</a>"
        "<a href='/files?path=" + quote(str(INBOX_DIR)) + "'><span>▣</span>文件</a>"
        "<a href='/settings'><span>⚙</span>设置</a>"
        "</nav>"
    )


def management_script(form_id):
    return (
        "<script>(function(){const form=document.getElementById('" + form_id + "'),"
        "cards=[...document.querySelectorAll('.manage-card')];if(!form)return;"
        "let timer,startX=0,startY=0,managing=false;"
        "function enter(card){managing=true;document.body.classList.add('managing');"
        "card.querySelector('input').checked=true;card.classList.add('selected');update();"
        "if(navigator.vibrate)navigator.vibrate(20);}"
        "function update(){cards.forEach(c=>c.classList.toggle('selected',c.querySelector('input').checked));"
        "document.querySelector('[data-count]').textContent="
        "cards.filter(c=>c.querySelector('input').checked).length+' 项';}"
        "function start(card,x,y){cancel();startX=x;startY=y;"
        "timer=setTimeout(()=>enter(card),500);}"
        "function cancel(){clearTimeout(timer);}"
        "cards.forEach(card=>{"
        "card.addEventListener('pointerdown',e=>{"
        "if(e.target.closest('input,button'))return;start(card,e.clientX,e.clientY);});"
        "['pointerup','pointercancel'].forEach(n=>card.addEventListener(n,cancel));"
        "card.addEventListener('pointermove',e=>{"
        "if(Math.abs(e.clientX-startX)>10||Math.abs(e.clientY-startY)>10)cancel();});"
        "card.addEventListener('click',e=>{if(!managing)return;e.preventDefault();"
        "const box=card.querySelector('input');box.checked=!box.checked;update()});"
        "card.addEventListener('contextmenu',e=>{"
        "if(managing||e.target.closest('.manage-card'))e.preventDefault()})});"
        "form.querySelector('[data-cancel]').onclick=()=>{managing=false;"
        "document.body.classList.remove('managing');"
        "cards.forEach(c=>c.querySelector('input').checked=false);update()};"
        "form.querySelector('[data-all]').onclick=()=>{"
        "const all=cards.every(c=>c.querySelector('input').checked);"
        "cards.forEach(c=>c.querySelector('input').checked=!all);update()};"
        "form.addEventListener('submit',e=>{"
        "const action=e.submitter.value,"
        "count=cards.filter(c=>c.querySelector('input').checked).length;"
        "if(!count){e.preventDefault();return}"
        "if(action==='delete'&&!confirm('确定删除选中的 '+count"
        "+' 项？此操作不可恢复。'))e.preventDefault()})})();</script>"
    )


def upload_dialog(directory, return_to, dialog_id="upload-dialog"):
    safe_directory = html_escape.escape(directory, quote=True)
    safe_return = html_escape.escape(return_to, quote=True)
    return (
        "<button class='attach-button' type='button' title='添加附件' aria-label='添加附件' "
        "onclick=\"document.getElementById('" + dialog_id + "').showModal()\">＋</button>"
        "<dialog id='" + dialog_id + "'>"
        "<h2>添加附件</h2>"
        "<form action='/upload' method='post' enctype='multipart/form-data'>"
        "<input type='hidden' name='directory' value='" + safe_directory + "'>"
        "<input type='hidden' name='return_to' value='" + safe_return + "'>"
        "<input name='files' type='file' multiple required>"
        "<p><small>可一次选择多个文件；每个文件上限 1000 MB。</small></p>"
        "<div class='dialog-actions'>"
        "<button type='button' class='secondary' onclick=\"this.closest('dialog').close()\">取消</button>"
        "<button>上传</button></div></form></dialog>"
    )


def safe_return_path(path, fallback="/home"):
    if path.startswith("/") and not path.startswith("//") and "\r" not in path and "\n" not in path:
        return path
    return fallback


def return_with_upload_notice(path, uploaded):
    separator = "&" if "?" in path else "?"
    return path + separator + "uploaded=" + quote(uploaded)


def upload_notice(filename):
    if not filename:
        return ""
    return (
        "<div class='notice' id='upload-notice'>附件已上传："
        "<b>" + html_escape.escape(filename) + "</b></div>"
        "<script>document.getElementById('upload-notice').scrollIntoView({block:'nearest'});</script>"
    )


def file_actions(path, is_directory):
    encoded = quote(str(path))
    codex = "/task/new-form?workdir=" + quote(str(path if is_directory else path.parent))
    if not is_directory:
        codex += "&prompt=" + quote("处理这个文件：" + path.name)
    actions = [
        ("交给 Codex", codex, ""),
        ("复制到", "/files/choose?action=copy&path=" + encoded, ""),
        ("移动到", "/files/choose?action=move&path=" + encoded, ""),
        ("重命名", "/move?path=" + encoded, ""),
        ("属性", "/files/properties?path=" + encoded, ""),
    ]
    if is_directory:
        actions.insert(0, ("打开", "/files?path=" + encoded, ""))
    if not is_directory:
        actions[1:1] = [
            ("下载", "/download?path=" + encoded, ""),
            ("编辑", "/edit?path=" + encoded, ""),
        ]
    actions.append(("删除", "/delete?path=" + encoded, "danger"))
    links = "".join(
        "<a class='" + style + "' href='" + href
        + "' onclick=\"this.closest('details').removeAttribute('open')\">"
        + label + "</a>"
        for label, href, style in actions
    )
    return (
        "<details class='file-actions' data-file-actions>"
        "<summary title='更多操作' aria-label='更多操作'>⋯</summary>"
        "<div class='file-actions-panel'>" + links + "</div></details>"
    )


def suggested_copy_name(source):
    if source.is_dir() or not source.suffix:
        return source.name + " - 副本"
    return source.stem + " - 副本" + source.suffix


def human_size(size):
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return (str(int(value)) if unit == "B" else f"{value:.1f}") + " " + unit
        value /= 1024


def status_label(status):
    return STATUS_LABELS.get(status, status)


def message_time(value):
    try:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone().strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return "时间未知"


def location_label(path):
    labels = {
        str(Path.home()): "我的 WSL 文件",
        str(INBOX_DIR): "收件箱",
        WINDOWS_DESKTOP_WSL: "Windows 桌面",
        "/mnt/d": "D 盘",
        WINDOWS_DESKTOP: "Windows 桌面",
        "D:": "D 盘",
    }
    return labels.get(path, path)


# ── Model / config validation ───────────────────────────────────────────────

def validate_task_config(model, reasoning, permission, agent=None):
    import model_catalog
    codex_models = {m["slug"] for m in model_catalog.get_models()}
    kimis = {m["slug"] for m in kimi_models()}
    if model not in codex_models and model not in kimis:
        raise ValueError("模型或权限无效。")
    if permission not in PERMISSIONS:
        raise ValueError("模型或权限无效。")
    if agent == "kimi" or model in kimis:
        return
    supported = model_catalog.get_supported_levels(model)
    if reasoning not in supported:
        raise ValueError("该模型不支持所选思考等级。")


def agent_select(form_id, suffix, current=None, locked=False):
    import tasks
    current = current if current in tasks.AGENT_CHOICES else tasks._default_agent()
    return (
        "<label>执行后端</label><select form='" + form_id + "' id='agent" + suffix
        + "' name='agent' onchange=\"updateAgent('" + suffix + "')\""
        + (" disabled" if locked else "") + ">"
        + "".join(
            "<option value='" + value + "'"
            + (" selected" if value == current else "") + ">"
            + label + "</option>"
            for value, label in (("codex", "Codex"), ("kimi", "Kimi"))
        )
        + "</select>"
    )


def _config_model_options(model, is_kimi):
    """Return (model_options_html, kimi_model_options_html)."""
    import model_catalog
    all_models = model_catalog.get_models()
    _kms = kimi_models()
    kd = kimi_default_model()
    model_options = "".join(
        "<option value='" + m["slug"] + "'"
        + (" selected" if m["slug"] == model else "")
        + " data-levels='" + json.dumps(m["levels"])
        + "' data-default-reasoning='" + m["default_reasoning"] + "'>"
        + m["display_name"] + "</option>"
        for m in all_models
    )
    kimi_model_options = "".join(
        "<option value='" + m["slug"] + "'"
        + (" selected" if m["slug"] == model else "") + ">"
        + m["display_name"] + "</option>"
        for m in _kms
    )
    return model_options, kimi_model_options, kd


def _kimi_thinking_options(current):
    """kimi ACP 思考档下拉选项：low/high/max，空或非法值按 high 选中（显式切换）。"""
    choices = [
        ("low", "Low"),
        ("high", "High"),
        ("max", "Max"),
    ]
    if current not in {"low", "high", "max"}:
        current = "high"
    return "".join(
        "<option value='" + value + "'" + (" selected" if value == current else "") + ">"
        + label + "</option>"
        for value, label in choices
    )


def _kimi_extra_controls(form_id, suffix, thinking, plan_mode):
    """kimi 专属控件：思考档下拉 + 计划模式勾选（仅 kimi agent 渲染）。"""
    thinking_id = "thinking" + suffix
    plan_id = "plan_mode" + suffix
    return (
        "<div class='kimi-only'>"
        + "<label>思考档</label><select form='" + form_id + "' id='"
        + thinking_id + "' name='thinking'>"
        + _kimi_thinking_options(thinking or "")
        + "</select>"
        + "<label><input form='" + form_id + "' id='" + plan_id
        + "' name='plan_mode' type='checkbox' value='1'"
        + (" checked" if plan_mode else "")
        + "> 计划模式</label>"
        + "</div>"
    )


def config_controls(form_id, model=None, reasoning="medium", permission="workspace-write",
                    suffix="", agent=None, agent_locked=False, thinking=None, plan_mode=False):
    import tasks
    def selected(value, current):
        return " selected" if value == current else ""
    is_kimi = (agent if agent in tasks.AGENT_CHOICES else tasks._default_agent()) == "kimi"
    _kms = {m["slug"] for m in kimi_models()}
    kd = kimi_default_model()
    if model is None:
        model = kd if is_kimi else model_catalog_get_default()
    elif is_kimi and model not in _kms:
        model = kd
    model_id = "model" + suffix
    reasoning_id = "reasoning" + suffix
    model_options, kimi_model_options, _kd = _config_model_options(model, is_kimi)
    reasoning_options = "".join(
        "<option value='" + level + "'" + (" selected" if level == reasoning else "") + ">"
        + level.title() + (" (Extra high)" if level == "xhigh" else "") + "</option>"
        for level in ALL_REASONING_LEVELS
    )
    return (
        "<details><summary>后端、模型、思考与执行权限</summary>"
        + agent_select(form_id, suffix, agent, agent_locked)
        + "<label>模型</label><select form='" + form_id + "' id='" + model_id
        + "' name='model' onchange=\"updateReasoning('" + suffix + "')\""
        + " data-kimi-options='" + html_escape.escape(kimi_model_options, quote=True)
        + "' data-kimi-default='" + html_escape.escape(kd, quote=True) + "'>"
        + model_options + "</select>"
        + "<div class='reasoning-wrap'><label>思考等级</label><select form='" + form_id
        + "' id='" + reasoning_id + "' name='reasoning'>"
        + reasoning_options + "</select></div>"
        + (_kimi_extra_controls(form_id, suffix, thinking, plan_mode) if is_kimi else "")
        + "<label>执行权限</label><select form='" + form_id + "' name='permission'>"
        + "<option value='workspace-write'" + selected("workspace-write", permission)
        + ">工作区可写</option>"
        + "<option value='read-only'" + selected("read-only", permission)
        + ">只读</option>"
        + "<option value='danger-full-access'" + selected("danger-full-access", permission)
        + ">完全访问（需要 PIN）</option></select></details>"
    )


def compact_config_controls(form_id, model, reasoning, permission, suffix="",
                            agent=None, agent_locked=False, thinking=None, plan_mode=False):
    import tasks
    def selected(value, current):
        return " selected" if value == current else ""
    is_kimi = (agent if agent in tasks.AGENT_CHOICES else tasks._default_agent()) == "kimi"
    _kms = {m["slug"] for m in kimi_models()}
    kd = kimi_default_model()
    original_model = model
    if model is None:
        model = kd if is_kimi else model_catalog_get_default()
    elif is_kimi and model not in _kms:
        model = kd
    model_options, kimi_model_options, _kd = _config_model_options(model, is_kimi)
    display = kimi_model_display_name(model) if is_kimi else model_catalog_get_display_name(model)
    codex_display = model_catalog_get_display_name(original_model) if original_model and original_model in {m["slug"] for m in _config_all_models()} else model_catalog_get_display_name(model_catalog_get_default())
    perm_label = {"workspace-write": "可写", "read-only": "只读", "danger-full-access": "完全访问"}.get(permission, permission)
    reasoning_label = "" if is_kimi else (" · " + reasoning.title())
    current = display + reasoning_label + " · " + perm_label
    agent_label = {"codex": "Codex", "kimi": "Kimi"}[agent if agent in tasks.AGENT_CHOICES else tasks._default_agent()]
    current = agent_label + " · " + current
    context = "<p class='config-context'>当前环境：WSL<br>默认目录：临时对话专用目录</p>" if suffix == "-quick" else ""
    advanced = "<a class='button secondary' href='/task/new-form'>高级：切换目录或 Windows</a>" if suffix == "-quick" else ""
    close = "<button class='secondary' type='button' onclick=\"this.closest('details').removeAttribute('open')\">完成</button>"
    summary_label = display if is_kimi else model_catalog_get_display_name(model)
    return (
        "<details class='composer-config'><summary data-codex-label='"
        + html_escape.escape(codex_display, quote=True)
        + "' data-kimi-label='" + html_escape.escape(kimi_model_display_name(model), quote=True)
        + "' title='后端、模型、思考与权限："
        + html_escape.escape(current, quote=True)
        + "' aria-label='后端、模型、思考与权限设置'>"
        + html_escape.escape(summary_label) + " ▾</summary>"
        + "<div class='composer-config-panel'><div class='config-panel-head'>"
        + "<strong>后端、模型、思考与权限</strong>" + close + "</div>"
        + context
        + agent_select(form_id, suffix, agent, agent_locked)
        + "<label>模型</label><select form='" + form_id + "' id='model" + suffix
        + "' name='model' onchange=\"updateReasoning('" + suffix + "')\""
        + " data-kimi-options='" + html_escape.escape(kimi_model_options, quote=True)
        + "' data-kimi-default='" + html_escape.escape(kd, quote=True) + "'>"
        + model_options + "</select>"
        + "<div class='reasoning-wrap'><label>思考等级</label><select form='" + form_id
        + "' id='reasoning" + suffix + "' name='reasoning'>"
        + "".join(
            "<option value='" + level + "'"
            + (" selected" if level == reasoning else "") + ">"
            + level.title() + (" (Extra high)" if level == "xhigh" else "") + "</option>"
            for level in ALL_REASONING_LEVELS
        )
        + "</select></div>"
        + (_kimi_extra_controls(form_id, suffix, thinking, plan_mode) if is_kimi else "")
        + "<label>执行权限</label><select form='" + form_id + "' name='permission'>"
        + "<option value='workspace-write'" + selected("workspace-write", permission)
        + ">工作区可写</option>"
        + "<option value='read-only'" + selected("read-only", permission)
        + ">只读</option>"
        + "<option value='danger-full-access'" + selected("danger-full-access", permission)
        + ">完全访问（需要 PIN）</option></select>"
        + advanced + "</div></details>"
    )


# Helpers for model_catalog lookups (so we don't import at module level)

def _config_all_models():
    import model_catalog
    return model_catalog.get_models()

def model_catalog_get_default():
    import model_catalog
    return model_catalog.get_default_model()

def model_catalog_get_display_name(slug):
    import model_catalog
    return model_catalog.get_display_name(slug)


# 提交后立即禁用按钮，防止连点造成重复提交（PIN 校验+跳转耗时，无反馈易误点）。
_PIN_SUBMIT_GUARD = (
    "var b=this.querySelector('button');"
    "if(b.disabled)return false;"
    "b.disabled=true;b.textContent='授权中，请稍候…';return true"
)


def full_access_pin_page(token, error=""):
    return html("完全访问授权",
        nav()
        + "<h1>完全访问授权</h1>"
        + "<p class='error'>Codex 可在当前系统权限范围内读写更多位置并执行命令。</p>"
        + ("<p class='error'>" + html_escape.escape(error) + "</p>" if error else "")
        + "<form action='/task/full-access' method='post' onsubmit=\"" + _PIN_SUBMIT_GUARD + "\">"
        + "<input type='hidden' name='token' value='"
        + html_escape.escape(token, quote=True) + "'>"
        + "<label>输入高风险 PIN</label>"
        + "<input name='pin' type='password' inputmode='numeric' required>"
        + "<button>授权并发送</button></form>"
    )


def windows_task_pin_page(token, error=""):
    return html("Windows 高风险授权",
        nav()
        + "<h1>Windows 高风险任务授权</h1>"
        + "<p class='error'>此授权仅对当前任务及当前工作目录有效，任务结束、失败或取消后自动失效。</p>"
        + ("<p class='error'>" + html_escape.escape(error) + "</p>" if error else "")
        + "<form action='/task/pin' method='post' onsubmit=\"" + _PIN_SUBMIT_GUARD + "\">"
        + "<input type='hidden' name='token' value='"
        + html_escape.escape(token, quote=True) + "'>"
        + "<label>输入 PIN</label>"
        + "<input name='pin' type='password' inputmode='numeric' pattern='[0-9]{4,12}' maxlength='12' required>"
        + "<button>授权并启动此任务</button></form>"
    )


# ── Conversation card helpers ───────────────────────────────────────────────

def _conversation_meta(item, dt=None, show_project=True):
    status = item.get("latest_status", item.get("status", ""))
    project_name = ""
    if show_project:
        project_name = item.get("project_name") or "临时对话"
        if item.get("project_id") and not item.get("project_name"):
            try:
                import projects
                project_name = projects.get(item["project_id"])["name"]
            except Exception:
                project_name = "—"
    pinned_mark = " ◉" if item.get("pinned") else ""
    status_dot = {
        "running": "●", "queued": "◌", "stopping": "◌",
        "waiting_approval": "◌", "windows_executing": "◌",
        "failed": "◉", "timed_out": "◉",
    }.get(status, "")
    sl = status_label(status) if status else ""
    meta_parts = []
    if status_dot:
        meta_parts.append("<span class='status-dot " + status + "'>"
                          + status_dot + " " + sl + "</span>")
    else:
        meta_parts.append("<span class='status-text'>" + sl + "</span>")
    if show_project:
        meta_parts.append(project_name)
    if pinned_mark:
        meta_parts.append("置顶")
    meta_text = " · ".join(meta_parts)
    time_str = dt.strftime("%H:%M") if dt else ""
    if time_str:
        meta_text = meta_text + (" · " + time_str if meta_text else time_str)
    return meta_text


def _conversation_card(item, form_id, dt=None, show_project=True):
    title = html_escape.escape(
        item.get("title") or item.get("prompt", "").replace("\n", " ")[:48]
    )
    meta = _conversation_meta(item, dt=dt, show_project=show_project)
    return (
        "<label class='manage-card' data-id='" + item["id"] + "'>"
        "<input class='manage-check' type='checkbox' form='" + form_id
        + "' name='ids' value='" + item["id"] + "'>"
        "<div class='conv-card'>"
        "<a class='conv-card-title' href='/task/" + item["latest_id"] + "'>"
        + title + "</a>"
        "<div class='conv-card-meta'>" + meta + "</div>"
        "</div></label>"
    )


def _conversation_groups(items, form_id, show_project=True):
    HKT = timezone(timedelta(hours=8))
    now = datetime.now(HKT)
    today = now.date()
    yesterday = today - timedelta(days=1)
    groups = {"today": [], "yesterday": [], "earlier": []}
    for item in items:
        activity = item.get("latest_activity", "") or ""
        try:
            dt = datetime.fromisoformat(activity.replace("Z", "+00:00")).astimezone(HKT)
            day = dt.date()
        except Exception:
            dt = None
            day = None
        if day == today:
            groups["today"].append((item, dt))
        elif day == yesterday:
            groups["yesterday"].append((item, dt))
        else:
            groups["earlier"].append((item, dt if day else None))
    rows = []
    for group_key, label in [
        ("today", "今天"), ("yesterday", "昨天"), ("earlier", "更早"),
    ]:
        group_items = groups[group_key]
        if not group_items:
            continue
        rows.append(
            "<div class='history-group'>"
            "<div class='history-group-head'>" + label + "</div>"
        )
        for item, dt in group_items:
            rows.append(_conversation_card(item, form_id, dt=dt, show_project=show_project))
        rows.append("</div>")
    return "".join(rows)
