"""File-browse, upload, edit, delete, move, and transfer routes."""
import html as html_escape
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse

from shared.config import (
    EDITS, FILE_ROOTS, INBOX_DIR, MAX_EDIT_BYTES, MAX_UPLOAD_BYTES, UPLOADS,
    WINDOWS_DESKTOP_WSL,
)
from shared.file_utils import (
    allowed_path, copy_directory_compatible, copy_file_compatible,
    editable_text, install_upload, sensitive_path,
    staged_edit, staged_upload,
)
from shared.html_lib import (
    file_actions as _file_actions, html, human_size, nav,
    return_with_upload_notice, safe_return_path, suggested_copy_name,
    upload_notice,
)
from shared.security import audit, confirmation_signer, notify, require_auth

router = APIRouter()


@router.get("/files")
def files(request: Request, path: str = ""):
    redirect = require_auth(request)
    if redirect:
        return redirect
    if not path:
        path = str(Path.home())
    try:
        directory = allowed_path(path, require_directory=True)
    except ValueError as error:
        return html("文件浏览",
            "<h1>文件浏览</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='./'>返回工作台</a>",
        )
    items = []
    if directory.parent != directory and any(
        directory.parent == root or root in directory.parent.parents for root in FILE_ROOTS
    ):
        items.append(
            "<li><a href='/files?path=" + quote(str(directory.parent))
            + "'>..（上级目录）</a></li>"
        )
    children = []
    skipped = 0
    for child in directory.iterdir():
        try:
            children.append((not child.is_dir(), child.name.lower(), child))
        except OSError:
            skipped += 1
    for _, _, child in sorted(children)[:500]:
        try:
            resolved = allowed_path(child)
            is_directory = resolved.is_dir()
        except (OSError, ValueError):
            skipped += 1
            continue
        name = html_escape.escape(child.name)
        if is_directory:
            items.append(
                "<li class='file-entry'><div class='file-entry-main'>"
                "<span class='file-name'>📁 <a href='/files?path="
                + quote(str(resolved)) + "'>" + name + "</a></span></div>"
                + _file_actions(resolved, True) + "</li>"
            )
        elif resolved.is_file():
            try:
                size = resolved.stat().st_size
            except OSError:
                skipped += 1
                continue
            items.append(
                "<li class='file-entry'><div class='file-entry-main'>"
                "<span class='file-name'>📄 " + name + "</span>"
                "<small> " + str(size) + " bytes</small></div>"
                + _file_actions(resolved, False) + "</li>"
            )
    note = "<p>已跳过 " + str(skipped) + " 个无法读取的系统条目。</p>" if skipped else ""
    upload_form = (
        "<h2>上传文件</h2>"
        "<form action='/upload' method='post' enctype='multipart/form-data'>"
        "<input type='hidden' name='directory' value='"
        + html_escape.escape(str(directory), quote=True) + "'>"
        "<input type='hidden' name='return_to' value='"
        + html_escape.escape("/files?path=" + quote(str(directory)), quote=True) + "'>"
        "<input name='files' type='file' multiple required>"
        "<button>上传</button></form>"
        "<p><small>可一次选择多个文件；每个文件上限 1000 MB。</small></p>"
    )
    import projects as _projects
    shortcuts = (
        "<div class='toolbar'>"
        "<a class='button' href='/files?path=" + quote(str(INBOX_DIR)) + "'>收件箱</a>"
        "<a class='button' href='/files?path=" + quote(str(_projects.ROOT)) + "'>项目</a>"
        "<a class='button' href='/files?path=" + quote(WINDOWS_DESKTOP_WSL) + "'>Windows 桌面</a>"
        "<a class='button' href='/files?path=/mnt/d'>D 盘</a></div>"
    )
    file_actions_script = (
        "<script>(function(){document.addEventListener('click',function(e){"
        "document.querySelectorAll('.file-actions[open]').forEach(function(d){"
        "if(!d.contains(e.target))d.removeAttribute('open');});});"
        "document.querySelectorAll('.file-actions').forEach(function(d){"
        "d.addEventListener('toggle',function(){if(!d.open)return;"
        "var p=d.querySelector('.file-actions-panel');if(!p)return;"
        "var r=d.getBoundingClientRect();"
        "var s=window.innerHeight-r.bottom;"
        "var h=Math.min(p.offsetHeight||240,320);"
        "p.classList.toggle('panel-up',s<h);});});})();</script>"
    )
    import shared.html_lib as hlib
    body = (
        nav()
        + hlib.templates.get_template("generic_page.html").render(
            title="",
            content=(
                "<h1>文件浏览</h1>" + shortcuts
                + "<p class='path'>当前目录：<code>"
                + html_escape.escape(str(directory)) + "</code></p>"
                + note + upload_form
                + "<ul class='file-list'>" + "".join(items) + "</ul>"
            ),
        )
    )
    return html("文件浏览", body + file_actions_script)


@router.get("/download")
def download(request: Request, path: str):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        file_path = allowed_path(path)
        if not file_path.is_file():
            raise ValueError("只能下载普通文件。")
    except ValueError as error:
        return html("下载失败",
            "<h1>下载失败</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='files'>返回文件浏览</a>",
        )
    try:
        with file_path.open("rb"):
            pass
    except OSError:
        return html("下载失败",
            "<h1>下载失败</h1>"
            "<p class='error'>该文件受 Windows 系统保护，当前账户无法读取。</p>"
            "<a href='/files'>返回文件浏览</a>",
        )
    audit(request, "file_downloaded", str(file_path))
    return FileResponse(file_path, filename=file_path.name)


@router.get("/files/properties")
def file_properties(request: Request, path: str):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        target = allowed_path(path)
        stat = target.stat()
        kind = "目录" if target.is_dir() else "文件"
        size = "—" if target.is_dir() else (
            human_size(stat.st_size) + "（" + str(stat.st_size) + " bytes）"
        )
        modified = datetime.fromtimestamp(stat.st_mtime).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError) as error:
        return html("无法读取属性",
            nav() + "<h1>无法读取属性</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='/files'>返回文件</a>",
        )
    values = (
        ("名称", target.name), ("类型", kind), ("大小", size),
        ("修改时间", modified), ("完整路径", str(target)),
    )
    details = "".join(
        "<dt>" + label + "</dt><dd>" + html_escape.escape(value) + "</dd>"
        for label, value in values
    )
    return html("属性",
        nav() + "<h1>属性</h1><dl class='property-grid'>" + details + "</dl>"
        + "<p><a href='/files?path=" + quote(str(target if target.is_dir() else target.parent))
        + "'>返回所在目录</a></p>",
    )


@router.get("/files/choose")
def choose_directory(request: Request, action: str, path: str, directory: str = ""):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        if action not in {"copy", "move"}:
            raise ValueError("操作无效。")
        source = allowed_path(path)
        current = allowed_path(directory, require_directory=True) if directory else source.parent
    except ValueError as error:
        return html("无法选择目录",
            nav() + "<h1>无法选择目录</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='/files'>返回文件</a>",
        )
    source_arg = quote(str(source))
    items = []
    parent_allowed = (
        current.parent != current
        and any(current.parent == root or root in current.parent.parents for root in FILE_ROOTS)
        and not sensitive_path(current.parent)
    )
    if parent_allowed:
        items.append(
            "<li><a href='/files/choose?action=" + action + "&path=" + source_arg
            + "&directory=" + quote(str(current.parent)) + "'>⬆ 上一级</a></li>"
        )
    try:
        children = sorted(
            (c for c in current.iterdir() if c.is_dir() and not sensitive_path(c.resolve())),
            key=lambda c: c.name.lower(),
        )
    except OSError:
        children = []
    for child in children[:300]:
        items.append(
            "<li><a href='/files/choose?action=" + action + "&path=" + source_arg
            + "&directory=" + quote(str(child.resolve())) + "'>📁 "
            + html_escape.escape(child.name) + "</a></li>"
        )
    import projects as _projects
    shortcuts = (
        "<div class='toolbar'>"
        "<a class='button secondary' href='/files/choose?action=" + action + "&path=" + source_arg
        + "&directory=" + quote(str(INBOX_DIR)) + "'>收件箱</a>"
        "<a class='button secondary' href='/files/choose?action=" + action + "&path=" + source_arg
        + "&directory=" + quote(str(_projects.ROOT)) + "'>项目</a>"
        "<a class='button secondary' href='/files/choose?action=" + action + "&path=" + source_arg
        + "&directory=" + quote(WINDOWS_DESKTOP_WSL) + "'>桌面</a>"
        "<a class='button secondary' href='/files/choose?action=" + action + "&path=" + source_arg
        + "&directory=/mnt/d'>D 盘</a></div>"
    )
    label = "复制到这里" if action == "copy" else "移动到这里"
    return html("选择目标文件夹",
        nav() + "<h1>选择目标文件夹</h1>"
        + "<p><small>来源：" + html_escape.escape(source.name) + "</small></p>"
        + shortcuts
        + "<p class='picker-current'>当前文件夹：<code>"
        + html_escape.escape(str(current)) + "</code></p>"
        + "<form action='/files/transfer/prepare' method='post'>"
        + "<input type='hidden' name='action' value='" + action + "'>"
        + "<input type='hidden' name='path' value='" + html_escape.escape(str(source), quote=True) + "'>"
        + "<input type='hidden' name='directory' value='" + html_escape.escape(str(current), quote=True) + "'>"
        + "<button>" + label + "</button></form>"
        + "<ul class='folder-picker'>" + "".join(items) + "</ul>",
    )


@router.post("/files/transfer/prepare")
def transfer_prepare(request: Request, action: str = Form(...),
                     path: str = Form(...), directory: str = Form(...)):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        if action not in {"copy", "move"}:
            raise ValueError("操作无效。")
        source = allowed_path(path)
        destination_dir = allowed_path(directory, require_directory=True)
        default_name = (
            suggested_copy_name(source) if action == "copy" and destination_dir == source.parent
            else source.name
        )
    except ValueError as error:
        return html("操作无效",
            nav() + "<h1>操作无效</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='/files'>返回文件</a>",
        )
    verb = "复制" if action == "copy" else "移动"
    return html(verb,
        nav() + "<h1>" + verb + "</h1>"
        + "<p>目标文件夹：<code>" + html_escape.escape(str(destination_dir)) + "</code></p>"
        + "<form action='/files/transfer/confirm' method='post'>"
        + "<input type='hidden' name='action' value='" + action + "'>"
        + "<input type='hidden' name='path' value='" + html_escape.escape(str(source), quote=True) + "'>"
        + "<input type='hidden' name='directory' value='" + html_escape.escape(str(destination_dir), quote=True) + "'>"
        + "<label>名称</label><input name='name' value='"
        + html_escape.escape(default_name, quote=True) + "' required>"
        + "<button>确认" + verb + "</button></form>",
    )


@router.post("/files/transfer/confirm")
def transfer_confirm(request: Request, action: str = Form(...),
                     path: str = Form(...), directory: str = Form(...), name: str = Form(...)):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        if action not in {"copy", "move"}:
            raise ValueError("操作无效。")
        source = allowed_path(path)
        destination_dir = allowed_path(directory, require_directory=True)
        filename = Path(name).name
        if not filename or filename in {".", ".."}:
            raise ValueError("名称无效。")
        destination = destination_dir / filename
        if source == destination:
            raise ValueError("目标与来源相同。")
        if destination_dir == source or source in destination_dir.parents:
            raise ValueError("不能把目录复制或移动到自身内部。")
        token = confirmation_signer().dumps({
            "action": action, "source": str(source), "destination": str(destination),
        })
        verb = "复制" if action == "copy" else "移动"
        return html("确认" + verb,
            nav() + "<h1>确认" + verb + "</h1>"
            + "<p>来源：<code>" + html_escape.escape(str(source)) + "</code></p>"
            + "<p>目标：<code>" + html_escape.escape(str(destination)) + "</code></p>"
            + "<form action='/files/transfer/execute' method='post'>"
            + "<input type='hidden' name='token' value='"
            + html_escape.escape(token, quote=True) + "'>"
            + "<button>确认执行</button></form>",
        )
    except ValueError as error:
        return html("操作无效",
            nav() + "<h1>操作无效</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='/files'>返回</a>",
        )


@router.post("/files/transfer/execute")
def transfer_execute(request: Request, token: str = Form(...)):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        from itsdangerous import BadSignature
        payload = confirmation_signer().loads(token, max_age=3600)
        source = allowed_path(payload["source"])
        destination = Path(payload["destination"])
        allowed_path(destination.parent, require_directory=True)
        if payload["action"] == "copy":
            copy_directory_compatible(source, destination) if source.is_dir() else copy_file_compatible(source, destination)
            event = "file_copied"
        elif payload["action"] == "move":
            import shutil
            shutil.move(str(source), str(destination))
            event = "file_moved"
        else:
            raise ValueError("操作无效。")
        audit(request, event, str(source) + " -> " + str(destination))
        notify(event, request.client.host if request.client else "unknown", str(destination))
        return html("操作成功",
            nav() + "<h1>操作成功</h1>"
            + "<p><code>" + html_escape.escape(str(destination)) + "</code></p>"
            + "<a href='/files?path=" + quote(str(destination.parent)) + "'>打开目标目录</a>",
        )
    except Exception:
        return html("操作失败",
            nav() + "<h1>操作失败</h1>"
            + "<p class='error'>目标已存在、权限不足、文件被占用或确认已失效。</p>"
            + "<a href='/files'>返回文件</a>",
        )


@router.get("/edit")
def edit(request: Request, path: str):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        file_path = allowed_path(path)
        content = editable_text(file_path)
    except ValueError as error:
        return html("无法编辑",
            "<h1>无法编辑</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='/files'>返回</a>",
        )
    return html("编辑文本",
        "<h1>编辑文本文件</h1><p><code>" + html_escape.escape(str(file_path)) + "</code></p>"
        + "<form action='/edit/confirm' method='post'>"
        + "<input type='hidden' name='path' value='" + html_escape.escape(str(file_path), quote=True) + "'>"
        + "<textarea name='content' rows='18' required>"
        + html_escape.escape(content) + "</textarea><button>进入写入确认</button></form>",
    )


@router.post("/edit/confirm")
def edit_confirm(request: Request, path: str = Form(...), content: str = Form(...)):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        file_path = allowed_path(path)
        editable_text(file_path)
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_EDIT_BYTES:
            raise ValueError("内容超过 1 MB 上限。")
        EDITS.mkdir(mode=0o700, parents=True, exist_ok=True)
        edit_id = secrets.token_hex(16)
        staged = EDITS / edit_id
        staged.write_text(content, encoding="utf-8")
        staged.chmod(0o600)
        payload = {"path": str(file_path), "edit_id": edit_id}
        token = confirmation_signer().dumps(payload)
    except ValueError as error:
        return html("无法编辑",
            "<h1>无法编辑</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='/files'>返回</a>",
        )
    return html("确认覆盖文本",
        "<h1>确认覆盖文本文件</h1>"
        + "<p class='error'>此操作将覆盖现有文件内容。</p>"
        + "<p><code>" + html_escape.escape(str(file_path)) + "</code></p>"
        + "<form action='/edit/write' method='post'>"
        + "<input type='hidden' name='token' value='"
        + html_escape.escape(token, quote=True) + "'>"
        + "<button>确认覆盖写入</button></form>",
    )


@router.post("/edit/write")
def edit_write(request: Request, token: str = Form(...)):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        payload = confirmation_signer().loads(token, max_age=3600)
        file_path = allowed_path(payload["path"])
        editable_text(file_path)
        staged = staged_edit(payload["edit_id"])
        content = staged.read_text(encoding="utf-8")
        if len(content.encode("utf-8")) > MAX_EDIT_BYTES:
            raise ValueError("内容超过上限。")
        temporary = file_path.with_name("." + file_path.name + ".codex-web-tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(file_path)
        staged.unlink(missing_ok=True)
        audit(request, "file_edited", str(file_path))
        notify("file_edited", request.client.host if request.client else "unknown", str(file_path))
        return html("写入成功",
            "<h1>写入成功</h1>"
            + "<p><code>" + html_escape.escape(str(file_path)) + "</code></p>"
            + "<a href='/files?path=" + quote(str(file_path.parent)) + "'>返回目录</a>",
        )
    except Exception:
        return html("写入失败",
            "<h1>写入失败</h1>"
            + "<p class='error'>文件可能已被更改、删除或权限不足。</p>"
            + "<a href='/files'>返回</a>",
        )


@router.get("/delete")
def delete(request: Request, path: str):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        target = allowed_path(path)
    except ValueError as error:
        return html("无法删除",
            "<h1>无法删除</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='/files'>返回</a>",
        )
    kind = "空目录" if target.is_dir() else "文件"
    token = confirmation_signer().dumps({"path": str(target)})
    return html("确认删除",
        "<h1>确认删除 " + kind + "</h1>"
        + "<p class='error'>删除后无法从工作台恢复。</p>"
        + "<p><code>" + html_escape.escape(str(target)) + "</code></p>"
        + "<form action='/delete/confirm' method='post'>"
        + "<input type='hidden' name='token' value='"
        + html_escape.escape(token, quote=True) + "'><button>继续</button></form>",
    )


@router.post("/delete/confirm")
def delete_confirm(request: Request, token: str = Form(...)):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        payload = confirmation_signer().loads(token, max_age=3600)
        target = allowed_path(payload["path"])
    except Exception:
        return html("删除确认失效", "<h1>删除确认失效</h1><a href='/files'>返回</a>")
    return html("再次确认删除",
        "<h1>再次确认删除</h1>"
        + "<p>请输入完整名称以确认：</p>"
        + "<p><code>" + html_escape.escape(target.name) + "</code></p>"
        + "<form action='/delete/execute' method='post'>"
        + "<input type='hidden' name='token' value='"
        + html_escape.escape(token, quote=True) + "'>"
        + "<input name='name' required placeholder='完整文件或目录名称'>"
        + "<button>永久删除</button></form>",
    )


@router.post("/delete/execute")
def delete_execute(request: Request, token: str = Form(...), name: str = Form(...)):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        payload = confirmation_signer().loads(token, max_age=3600)
        target = allowed_path(payload["path"])
        if name != target.name:
            raise ValueError("名称不匹配。")
        parent = target.parent
        if target.is_dir():
            target.rmdir()
        else:
            target.unlink()
        audit(request, "file_deleted", str(target))
        notify("file_deleted", request.client.host if request.client else "unknown", str(target))
        return html("删除成功",
            "<h1>删除成功</h1>"
            + "<a href='/files?path=" + quote(str(parent)) + "'>返回目录</a>",
        )
    except OSError:
        return html("删除失败",
            "<h1>删除失败</h1>"
            + "<p class='error'>目录非空、文件受保护或权限不足。递归删除未开放。</p>"
            + "<a href='/files'>返回</a>",
        )
    except Exception:
        return html("删除失败",
            "<h1>删除失败</h1>"
            + "<p class='error'>确认无效或名称不匹配。</p>"
            + "<a href='/files'>返回</a>",
        )


@router.get("/move")
def move(request: Request, path: str):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        source = allowed_path(path)
    except ValueError as error:
        return html("无法移动",
            "<h1>无法移动</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='/files'>返回</a>",
        )
    return html("移动或重命名",
        "<h1>移动或重命名</h1>"
        + "<p>来源：<code>" + html_escape.escape(str(source)) + "</code></p>"
        + "<form action='/move/confirm' method='post'>"
        + "<input type='hidden' name='path' value='" + html_escape.escape(str(source), quote=True) + "'>"
        + "<label>目标目录</label><input name='directory' value='"
        + html_escape.escape(str(source.parent), quote=True) + "' required>"
        + "<label>新名称</label><input name='name' value='"
        + html_escape.escape(source.name, quote=True) + "' required>"
        + "<button>进入确认</button></form>",
    )


@router.post("/move/confirm")
def move_confirm(request: Request, path: str = Form(...),
                 directory: str = Form(...), name: str = Form(...)):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        source = allowed_path(path)
        destination_dir = allowed_path(directory, require_directory=True)
        filename = Path(name).name
        if not filename or filename in {".", ".."}:
            raise ValueError("新名称无效。")
        destination = destination_dir / filename
        if source == destination:
            raise ValueError("目标与来源相同。")
        payload = {"source": str(source), "destination": str(destination), "overwrite": False}
        token = confirmation_signer().dumps(payload)
    except ValueError as error:
        return html("无法移动",
            "<h1>无法移动</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='/files'>返回</a>",
        )
    warning = (
        "<p class='error'>目标已存在，下一步会要求再次确认覆盖。</p>"
        if destination.exists() else ""
    )
    return html("确认移动",
        "<h1>确认移动或重命名</h1>"
        + "<p>来源：<code>" + html_escape.escape(str(source)) + "</code></p>"
        + "<p>目标：<code>" + html_escape.escape(str(destination)) + "</code></p>"
        + warning
        + "<form action='/move/execute' method='post'>"
        + "<input type='hidden' name='token' value='"
        + html_escape.escape(token, quote=True) + "'>"
        + "<button>确认执行</button></form>",
    )


@router.post("/move/execute")
def move_execute(request: Request, token: str = Form(...)):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        import shutil
        payload = confirmation_signer().loads(token, max_age=3600)
        source = allowed_path(payload["source"])
        destination = Path(payload["destination"])
        allowed_path(destination.parent, require_directory=True)
        if destination.exists() and not payload.get("overwrite"):
            if source.is_dir() != destination.is_dir():
                raise ValueError("不能用文件覆盖目录或用目录覆盖文件。")
            payload["overwrite"] = True
            return html("再次确认覆盖",
                "<h1>再次确认覆盖</h1>"
                + "<p class='error'>目标已存在，将被覆盖。</p>"
                + "<p><code>" + html_escape.escape(str(destination)) + "</code></p>"
                + "<form action='/move/execute' method='post'>"
                + "<input type='hidden' name='token' value='"
                + html_escape.escape(confirmation_signer().dumps(payload), quote=True) + "'>"
                + "<button>确认覆盖并移动</button></form>",
            )
        if destination.exists():
            if destination.is_dir():
                destination.rmdir()
            else:
                destination.unlink()
        shutil.move(str(source), str(destination))
        audit(request, "file_moved", str(source) + " -> " + str(destination))
        notify("file_moved", request.client.host if request.client else "unknown", str(destination))
        return html("移动成功",
            "<h1>移动成功</h1>"
            + "<p><code>" + html_escape.escape(str(destination)) + "</code></p>"
            + "<a href='/files?path=" + quote(str(destination.parent)) + "'>打开目标目录</a>",
        )
    except Exception:
        return html("移动失败",
            "<h1>移动失败</h1>"
            + "<p class='error'>权限不足、目标目录非空或文件被占用。</p>"
            + "<a href='/files'>返回</a>",
        )


@router.post("/upload")
def upload(request: Request, directory: str = Form(...),
           files: list[UploadFile] = File(...), return_to: str = Form("")):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        target_dir = allowed_path(directory, require_directory=True)
    except ValueError as error:
        return html("上传失败",
            "<h1>上传失败</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='/files'>返回</a>",
        )
    staged_files = []
    total_size = 0
    UPLOADS.mkdir(mode=0o700, parents=True, exist_ok=True)
    for upload_file in files:
        name = upload_file.filename or "untitled"
        upload_id = secrets.token_hex(12)
        staged_path = UPLOADS / upload_id
        try:
            with staged_path.open("wb") as out:
                while True:
                    chunk = upload_file.file.read(65536)
                    if not chunk:
                        break
                    total_size += len(chunk)
                    if total_size > MAX_UPLOAD_BYTES:
                        raise ValueError("上传文件总大小超过 1000 MB 上限。")
                    out.write(chunk)
        finally:
            upload_file.file.close()
        staged_path.chmod(0o600)
        staged_files.append({
            "name": name, "upload_id": upload_id,
            "destination": str(target_dir / name),
        })
    if not staged_files:
        return html("上传失败",
            "<h1>上传失败</h1>"
            + "<p class='error'>未选择文件或文件名为空。</p>"
            + "<a href='/files'>返回</a>",
        )
    save_args = {"files": staged_files, "return_to": safe_return_path(return_to)}
    token = confirmation_signer().dumps(save_args)
    return html("确认上传",
        nav() + "<h1>确认上传文件</h1>"
        + "<p>目标目录：<code>" + html_escape.escape(str(target_dir)) + "</code></p>"
        + "<ul>" + "".join(
            "<li>" + html_escape.escape(f["name"])
            + " <small>(" + str(Path(f["name"]).suffix) + ")</small></li>"
            for f in staged_files
        ) + "</ul>"
        + "<form action='/upload/confirm' method='post'>"
        + "<input type='hidden' name='token' value='"
        + html_escape.escape(token, quote=True) + "'>"
        + "<button>确认上传</button></form>",
    )


@router.post("/upload/confirm")
def upload_confirm(request: Request, token: str = Form(...)):
    redirect = require_auth(request)
    if redirect:
        return redirect
    try:
        payload = confirmation_signer().loads(token, max_age=3600)
        destination = None
        for file_info in payload["files"]:
            dest = Path(file_info["destination"])
            allowed_path(dest.parent, require_directory=True)
            install_upload(staged_upload(file_info["upload_id"]), dest)
            destination = dest
        audit(request, "files_uploaded", str(destination.parent if destination else ""))
        return_to = safe_return_path(payload.get("return_to", "/files"))
        if destination:
            return_to = return_with_upload_notice(return_to, destination.name)
        return RedirectResponse(return_to, 303)
    except Exception as error:
        return html("上传失败",
            "<h1>上传失败</h1><p class='error'>"
            + html_escape.escape(str(error)) + "</p><a href='/files'>返回</a>",
        )
