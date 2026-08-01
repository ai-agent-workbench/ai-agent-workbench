"""Authentication, session signing, audit logging, and push notifications."""
import json
import hashlib
import hmac
import secrets
import urllib.request
from datetime import datetime, timezone

import pyotp
from itsdangerous import BadSignature, URLSafeTimedSerializer

from shared.config import (
    ACCOUNT, AUDIT_FILE, ENABLED_FILE, MAX_AGE, NTFY_TOPIC_FILE,
    SECRET_FILE, SESSION_FILE, STATE, WINDOWS_PIN_FILE,
)


def _saved_secret(path, factory):
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(factory(), encoding="ascii")
        path.chmod(0o600)
    return path.read_text(encoding="ascii").strip()


def totp():
    return pyotp.TOTP(_saved_secret(SECRET_FILE, pyotp.random_base32))


def signer():
    return URLSafeTimedSerializer(
        _saved_secret(SESSION_FILE, lambda: secrets.token_urlsafe(48)),
        salt="codex-web",
    )


def confirmation_signer():
    return URLSafeTimedSerializer(
        _saved_secret(SESSION_FILE, lambda: secrets.token_urlsafe(48)),
        salt="codex-web-task-confirm",
    )


def pin_record(pin=None):
    if pin is not None:
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, 310000)
        WINDOWS_PIN_FILE.write_text(salt.hex() + ":" + digest.hex(), encoding="ascii")
        WINDOWS_PIN_FILE.chmod(0o600)
    try:
        salt_hex, digest_hex = WINDOWS_PIN_FILE.read_text(encoding="ascii").strip().split(":")
        return bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)
    except (OSError, ValueError):
        raise ValueError("Windows PIN 未设置。")


def valid_windows_pin(pin):
    salt, expected = pin_record()
    actual = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, 310000)
    return hmac.compare_digest(actual, expected)


def windows_high_risk(prompt):
    lowered = prompt.lower()
    readonly_phrases = (
        "只分析", "只读", "不要修改", "不修改", "不写入", "不删除", "不运行",
        "do not modify", "do not write", "do not delete", "do not run",
        "read-only", "only analyze", "only report",
    )
    if any(phrase in lowered for phrase in readonly_phrases):
        return False
    terms = (
        "write", "modify", "edit", "delete", "remove", "overwrite", "rename", "move",
        "install", "uninstall", "execute", "run", "launch", "powershell", "cmd",
        "registry", "service", "firewall", "scheduled task", "download", "create",
        "写入", "修改", "编辑", "删除", "覆盖", "重命名", "移动", "安装", "卸载",
        "执行", "运行", "启动", "注册表", "服务", "防火墙", "计划任务", "下载", "创建",
    )
    return any(term in lowered for term in terms)


def audit(request, event, detail=""):
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = {
        "time": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "ip": request.client.host if request.client else "unknown",
        "detail": str(detail),
    }
    with AUDIT_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=True) + "\n")
    AUDIT_FILE.chmod(0o600)
    notify(event, record["ip"], str(detail))


def notify_message(title, body, priority="default", tags="lock"):
    if not NTFY_TOPIC_FILE.exists():
        return
    topic = NTFY_TOPIC_FILE.read_text(encoding="ascii").strip()
    # 请求头仅支持 latin-1，中文标题需按 RFC 2047 编码，否则 http.client 抛
    # UnicodeEncodeError（曾被下方 except 静默吞掉，导致 07-22 标题中文化后通知全灭）。
    if not title.isascii():
        import base64
        title = "=?UTF-8?B?" + base64.b64encode(title.encode("utf-8")).decode("ascii") + "?="
    req = urllib.request.Request(
        "https://ntfy.sh/" + topic,
        data=body.encode("utf-8"),
        headers={"Title": title, "Priority": priority, "Tags": tags},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=8).read()
    except Exception:
        pass


_SECURITY_ALERTS = {
    "totp_login_failed": ("登录失败", "有人尝试登录工作台，如非本人请检查安全", "high", "lock"),
    "totp_enabled": ("验证器已启用", "双因素认证已开启", "default", "lock"),
    "full_access_conversation_started": ("全权限对话", "有人启动了全权限对话", "default", "lock"),
    "windows_task_pin_failed": ("PIN 错误", "Windows 任务 PIN 验证失败", "high", "lock"),
    "windows_bridge_pin_failed": ("PIN 错误", "桥接审批 PIN 验证失败", "high", "lock"),
    "windows_approval_required": ("操作待审批", "有操作等待你审批", "high", "hourglass"),
    "file_deleted": ("文件已删除", "有文件或目录被删除", "high", "warning"),
}


def notify(event, ip, detail=""):
    message = _SECURITY_ALERTS.get(event)
    if message is None:
        return
    title, body, priority, tags = message
    parts = [body]
    if detail:
        parts.append(str(detail)[:200])
    if ip and ip not in {"background", "unknown"}:
        parts.append("来源：" + ip)
    notify_message(title, "\n".join(parts), priority, tags)


def notify_task_terminal(metadata):
    if metadata.get("status") != "waiting_approval":
        return
    import tasks
    request = tasks.approval_snapshot(metadata.get("id", "")) or {}
    source = request.get("source", "windows")
    source_label = "本地操作" if source == "local" else "Windows 操作"
    title = str(metadata.get("title") or metadata.get("user_prompt") or source_label)
    summary = str(request.get("summary") or title)[:160]
    notify("windows_approval_required", "background", summary)


def logged_in(request):
    token = request.cookies.get("codex_web_session")
    if not token:
        return False
    try:
        return signer().loads(token, max_age=MAX_AGE) == ACCOUNT
    except BadSignature:
        return False


def require_auth(request):
    from fastapi.responses import RedirectResponse
    if not ENABLED_FILE.exists():
        return RedirectResponse("/setup", 303)
    if not logged_in(request):
        return RedirectResponse("/login", 303)
    return None
