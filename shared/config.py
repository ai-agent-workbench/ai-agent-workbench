"""Central configuration — all constants, paths, and env-var bindings.

Every module under shared/ and routes/ imports what it needs from here.
No route or helper should duplicate a path literal that already lives here.
"""
import os
from datetime import timedelta
from pathlib import Path


# ── State directory ─────────────────────────────────────────────────────────

STATE = Path(os.environ.get(
    "CODEX_WEB_STATE_DIR",
    str(Path.home() / ".local" / "share" / "codex-web"),
))
SECRET_FILE = STATE / "totp_secret"
ENABLED_FILE = STATE / "totp_enabled"
SESSION_FILE = STATE / "session_secret"
WINDOWS_PIN_FILE = STATE / "windows_high_risk_pin"
AUDIT_FILE = STATE / "audit.jsonl"
NTFY_TOPIC_FILE = Path(os.environ.get(
    "CODEX_WEB_NTFY_TOPIC_FILE",
    str(STATE / "ntfy-topic.txt"),
))

# 登录账号名：必须通过环境变量注入，不写死在代码里。
_account = os.environ.get("WORKBENCH_ACCOUNT", "").strip()
if not _account:
    raise RuntimeError(
        "WORKBENCH_ACCOUNT 环境变量未设置。请将其配置为你的登录账号名"
        "（见 README「环境变量」一节或 config.example.sh）。"
    )
ACCOUNT = _account
MAX_AGE = int(timedelta(days=7).total_seconds())

UPLOADS = STATE / "uploads"
EDITS = STATE / "edits"

MAX_UPLOAD_BYTES = 1000 * 1024 * 1024
MAX_EDIT_BYTES = 1024 * 1024

# ── File-access boundaries ──────────────────────────────────────────────────
# CODEX_WEB_FILE_ROOTS / CODEX_WEB_SENSITIVE_PATHS：用 os.pathsep（Linux 为 :）
# 分隔多个路径；未设置时回落到运行用户的通用默认值。

_file_roots_env = os.environ.get("CODEX_WEB_FILE_ROOTS", "")
if _file_roots_env.strip():
    FILE_ROOTS = tuple(
        Path(p).expanduser().resolve()
        for p in _file_roots_env.split(os.pathsep)
        if p.strip()
    )
else:
    FILE_ROOTS = (Path.home().resolve(),)

_sensitive_env = os.environ.get("CODEX_WEB_SENSITIVE_PATHS", "")
if _sensitive_env.strip():
    SENSITIVE_PATHS = tuple(
        Path(p).expanduser().resolve()
        for p in _sensitive_env.split(os.pathsep)
        if p.strip()
    )
else:
    SENSITIVE_PATHS = (
        (Path.home() / ".ssh").resolve(),
        (Path.home() / ".codex").resolve(),
        STATE.resolve(),
        Path(__file__).resolve().parent.parent,  # 应用目录本身
    )

# ── Per-environment helper paths ────────────────────────────────────────────

INBOX_DIR = Path(os.environ.get(
    "CODEX_WEB_INBOX_DIR",
    str(Path.home() / "Inbox"),
))

# Windows 侧允许操作的桌面目录（WSL 视图与 Windows 原生写法各一份）。
WINDOWS_DESKTOP_WSL = os.environ.get(
    "CODEX_WEB_WINDOWS_DESKTOP_WSL",
    "/mnt/c/Users/user/Desktop",
)
WINDOWS_DESKTOP = os.environ.get(
    "CODEX_WEB_WINDOWS_DESKTOP",
    r"C:\Users\user\Desktop",
)

# ── Model / reasoning / permission enums ────────────────────────────────────

ALL_REASONING_LEVELS = ("low", "medium", "high", "xhigh", "max", "ultra")
PERMISSIONS = ("read-only", "workspace-write", "danger-full-access")

# ── Status labels ───────────────────────────────────────────────────────────

STATUS_LABELS = {
    "queued": "等待中",
    "running": "运行中",
    "stopping": "正在停止",
    "waiting_approval": "等待授权",
    "windows_executing": "执行中",
    "windows_execution_uncertain": "结果待复核",
    "succeeded": "已完成",
    "failed": "失败",
    "cancelled": "已停止",
    "timed_out": "已超时",
    "finished": "已结束",
}

# ── Kimi config ─────────────────────────────────────────────────────────────

KIMI_CONFIG_PATH = Path(os.environ.get(
    "CODEX_WEB_KIMI_CONFIG_PATH",
    str(Path.home() / ".kimi-code-web" / "config.toml"),
))


def kimi_models():
    """Return kimi model choices from its config.toml, with a hardcoded fallback."""
    import tomllib
    try:
        with open(KIMI_CONFIG_PATH, "rb") as fh:
            config = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        config = {}
    models = []
    for slug, info in config.get("models", {}).items():
        models.append({"slug": slug, "display_name": info.get("display_name", slug)})
    if not models:
        models = [
            {"slug": "kimi-code/k3-256k", "display_name": "K3 256K"},
            {"slug": "kimi-code/k3", "display_name": "K3"},
            {"slug": "kimi-code/kimi-for-coding", "display_name": "K2.7 Coding"},
            {"slug": "kimi-code/kimi-for-coding-highspeed", "display_name": "K2.7 Coding Highspeed"},
        ]
    order = ["kimi-code/k3-256k", "kimi-code/k3", "kimi-code/kimi-for-coding", "kimi-code/kimi-for-coding-highspeed"]
    models_map = {m["slug"]: m for m in models}
    ordered = [models_map[s] for s in order if s in models_map]
    ordered.extend([m for m in models if m["slug"] not in order])
    return ordered


def kimi_default_model():
    import tomllib
    try:
        with open(KIMI_CONFIG_PATH, "rb") as fh:
            config = tomllib.load(fh)
        default = config.get("default_model", "")
        if default:
            return default
    except (OSError, tomllib.TOMLDecodeError):
        pass
    models = kimi_models()
    return models[0]["slug"] if models else ""


def kimi_default_thinking():
    """kimi ACP thinking 工作台级默认：读 config.toml [workbench] default_thinking，缺省 high。

    显式切换语义（适配层对非空值一律发 set_config_option）：默认 high 比
    CLI 自带默认（0.29.2 实测实为 max）省 token，且行为确定不随版本漂移。
    """
    import tomllib
    try:
        with open(KIMI_CONFIG_PATH, "rb") as fh:
            config = tomllib.load(fh)
        value = str(config.get("workbench", {}).get("default_thinking", "")).strip().lower()
    except (OSError, tomllib.TOMLDecodeError):
        return "high"
    return value if value in {"low", "high", "max"} else "high"


def kimi_engine():
    """kimi 执行引擎：spawn（一次性进程，默认）| acp（ACP 会话，真历史+秒停）。

    读 KIMI_CONFIG_PATH（默认 ~/.kimi-code-web/config.toml）的 [workbench] kimi_engine。
    每次 start() 现读 —— 改一行配置立即生效，无需重启服务；
    这就是 ACP 接线的一键回退开关。缺省/非法值一律回落 spawn。
    """
    import tomllib
    try:
        with open(KIMI_CONFIG_PATH, "rb") as fh:
            config = tomllib.load(fh)
        value = str(config.get("workbench", {}).get("kimi_engine", "")).strip().lower()
    except (OSError, tomllib.TOMLDecodeError):
        return "spawn"
    return value if value in {"spawn", "acp"} else "spawn"


def kimi_model_display_name(slug):
    for m in kimi_models():
        if m["slug"] == slug:
            return m["display_name"]
    return slug
