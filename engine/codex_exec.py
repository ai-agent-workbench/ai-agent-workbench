"""Build CLI command-lists for codex and kimi backends.

Extracted from tasks.py:start() so the exec logic is testable and
reusable.  The Kimi dedicated path lives here; codex exec remains as
fallback.

Call-pattern:
    command, env, cwd = build_exec_command(metadata)
    process = subprocess.Popen(command, env=env, cwd=cwd, ...)
"""

import base64
import json
import os
import shutil
import sys
from pathlib import Path

from shared.config import STATE as _STATE_DIR

# ── Paths shared with tasks.py ──────────────────────────────────────────────

KIMI_COMMAND = os.environ.get(
    "CODEX_WEB_KIMI_COMMAND",
    str(Path.home() / ".kimi-code" / "bin" / "kimi"),
)
KIMI_CODE_HOME = os.environ.get("KIMI_CODE_HOME", str(Path.home() / ".kimi-code-web"))
CODEX_WEB_PORT = os.environ.get("CODEX_WEB_PORT", "18080")

WINDOWS_KEY = os.environ.get("CODEX_WEB_WINDOWS_KEY", str(Path.home() / ".ssh" / "windows_ed25519"))
WINDOWS_HOST = os.environ.get("CODEX_WEB_WINDOWS_HOST", "user@127.0.0.1")
WINDOWS_SSH_PORT = os.environ.get("CODEX_WEB_WINDOWS_SSH_PORT", "58231")
WINDOWS_TASKS = os.environ.get("CODEX_WEB_WINDOWS_TASKS", r"C:\Users\user\codex-web\tasks")

# ── Kimi environment note (injected into sandboxed prompts) ──────────────────

_KIMI_ENV_NOTE = (
    "[运行环境] 你是网页工作台发起的任务，运行在沙盒中：不能读写工作目录之外的文件，"
    "也没有直接 SSH 到 Windows 的密钥（该通道仅用户终端可用）。"
    "Windows 操作请使用 mcp__codex-web-bridge__windows_inspect（只读检查，直接执行）"
    "或 mcp__codex-web-bridge__windows_execute（写操作，将请用户 PIN 审批后执行）；"
    "重启本工作台等本机操作用 mcp__codex-web-bridge__local_execute（同样 PIN 审批）。\n\n"
)


def build_exec_command(metadata: dict) -> tuple[list[str], dict | None, str | None]:
    """Return (command_list, env_dict_or_None, cwd_or_None) for *metadata*."""
    target = metadata.get("target", "wsl")
    agent = metadata.get("agent", "codex")

    if target == "windows":
        return _windows_command(metadata), None, None

    if agent == "kimi":
        return _kimi_command(metadata)

    return _codex_command(metadata), None, None


# ── Windows (SSH + codex exec on remote host) ───────────────────────────────

def _windows_command(metadata: dict) -> list[str]:
    workdir = metadata["workdir"]
    prompt = metadata["prompt"]
    model = metadata.get("model")
    reasoning = metadata.get("reasoning", "medium")

    payload = base64.b64encode(
        json.dumps({
            "workdir": workdir, "prompt": prompt,
            "model": model, "reasoning": reasoning,
            "permission": metadata.get("permission", "workspace-write"),
        }, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")

    ps_args = "','".join([
        "exec", "--json", "--skip-git-repo-check",
        "-s", metadata.get("permission", "workspace-write"),
        "-C", workdir,
        "-c", f'model_reasoning_effort="{reasoning}"',
    ])
    if model:
        ps_args += f"','-m','{model}"
    ps_args += f"','--output-last-message','{WINDOWS_TASKS}\\{metadata['id']}\\final.txt','{prompt}'"

    script = (
        f"$payload=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}'))|ConvertFrom-Json;"
        f"$dir='{WINDOWS_TASKS}\\{metadata['id']}';New-Item -ItemType Directory -Force -Path $dir|Out-Null;"
        f"$args=@('{ps_args}');& codex @args"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")

    return [
        "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-i", WINDOWS_KEY, "-p", WINDOWS_SSH_PORT, WINDOWS_HOST,
        "powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded,
    ]


# ── Codex (local codex exec with MCP bridge) ────────────────────────────────

def _codex_command(metadata: dict) -> list[str]:
    task_id = metadata["id"]
    bridge_args = json.dumps([
        str(Path(__file__).with_name("..").resolve() / "windows_bridge.py"),
        "--task-id", task_id,
    ])
    command = [
        "codex", "exec", "--json", "--skip-git-repo-check",
        "-c", "approval_policy=\"never\"",
        "-c", f"mcp_servers.windows_bridge.command={json.dumps(sys.executable)}",
        "-c", f"mcp_servers.windows_bridge.args={bridge_args}",
        "-c", "mcp_servers.windows_bridge.required=true",
        "-c", "mcp_servers.windows_bridge.enabled_tools=[\"windows_inspect\",\"windows_execute\"]",
        "-c", "mcp_servers.windows_bridge.default_tools_approval_mode=\"approve\"",
        "-c", "mcp_servers.windows_bridge.tools.windows_execute.approval_mode=\"approve\"",
        "-s", metadata.get("permission", "workspace-write"),
        "-C", metadata["workdir"],
    ]
    if metadata.get("model"):
        command[2:2] = ["-m", metadata["model"]]
    if metadata.get("reasoning"):
        command[2:2] = ["-c", f'model_reasoning_effort="{metadata["reasoning"]}"']
    command.extend([
        "--output-last-message",
        str(Path(_STATE_DIR) / "tasks" / task_id / "final.txt"),
        metadata["prompt"],
    ])
    return command


# ── Kimi (local kimi CLI, sandboxed with bwrap when available) ───────────────

def _kimi_command(metadata: dict) -> tuple[list[str], dict, str]:
    permission = metadata.get("permission", "workspace-write")
    sandboxed = permission != "danger-full-access" and _bwrap_available()
    prompt = _KIMI_ENV_NOTE + metadata["prompt"] if sandboxed else metadata["prompt"]

    command = [KIMI_COMMAND, "--output-format", "stream-json", "-p", prompt]
    if metadata.get("model"):
        command.extend(["-m", metadata["model"]])

    env = dict(
        os.environ,
        PYTHONUNBUFFERED="1",
        KIMI_CODE_HOME=KIMI_CODE_HOME,
    )

    if sandboxed:
        _write_user_mcp_config(metadata["id"])
        command = _bwrap_argv(metadata["workdir"], permission) + command

    return command, env, metadata["workdir"]


# ── bwrap helpers ───────────────────────────────────────────────────────────

def _bwrap_available() -> bool:
    return shutil.which("bwrap") is not None


def _bwrap_argv(workdir: str, permission: str) -> list[str]:
    workdir_ro = permission == "read-only"
    home_dir = str(Path.home())
    argv = [
        "bwrap",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/lib", "/lib",
        "--ro-bind", "/lib64", "/lib64",
        "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/etc", "/etc",
        "--ro-bind", "/mnt/wsl/resolv.conf", "/mnt/wsl/resolv.conf",
        "--tmpfs", home_dir,
        "--dir", str(Path.home() / ".kimi-code" / "bin"),
        "--ro-bind", KIMI_COMMAND, KIMI_COMMAND,
        "--bind", KIMI_CODE_HOME, KIMI_CODE_HOME,
    ]
    if workdir_ro:
        argv.extend(["--ro-bind", workdir, workdir])
    else:
        argv.extend(["--bind", workdir, workdir])
    argv.extend([
        "--remount-ro", home_dir,
        "--dir", "/tmp",
        "--proc", "/proc",
        "--dev", "/dev",
        "--unshare-pid",
        "--die-with-parent",
        "--new-session",
        "--setenv", "HOME", "/tmp/home",
        "--setenv", "KIMI_CODE_HOME", KIMI_CODE_HOME,
    ])
    return argv


def _user_mcp_path() -> Path:
    return Path(KIMI_CODE_HOME) / "mcp.json"


def _write_user_mcp_config(task_id: str) -> None:
    config = {
        "mcpServers": {
            "codex-web-bridge": {
                "url": f"http://127.0.0.1:{CODEX_WEB_PORT}/bridge/mcp/{task_id}"
            }
        }
    }
    path = _user_mcp_path()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        path.chmod(0o600)
    except OSError:
        pass
