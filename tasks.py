import json
import time
import base64
import os
import re
import secrets
import subprocess
import signal
import shutil
import threading
import sys
import html as html_escape
import concurrent.futures
from datetime import datetime, timezone
from pathlib import Path

import windows_bridge
import projects

from routes.ws import manager, push_threadsafe
from shared.config import INBOX_DIR, kimi_default_model, kimi_default_thinking, kimi_engine
from engine import kimi_acp

STATE_DIR = Path(os.environ.get(
    "CODEX_WEB_STATE_DIR",
    str(Path.home() / ".local" / "share" / "codex-web"),
))
ROOT = STATE_DIR / "tasks"
AUDIT_FILE = STATE_DIR / "audit.jsonl"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
AGENT_STATE_FILE = STATE_DIR / "agent"
KIMI_COMMAND = os.environ.get("CODEX_WEB_KIMI_COMMAND", str(Path.home() / ".kimi-code" / "bin" / "kimi"))
KIMI_CODE_HOME = os.environ.get("CODEX_WEB_KIMI_CODE_HOME", str(Path.home() / ".kimi-code-web"))
KIMI_SESSIONS_DIR = Path(KIMI_CODE_HOME) / "sessions"
KIMI_EXTERNAL_SESSIONS_DIR = Path.home() / ".kimi-code" / "sessions"
_KIMI_CONTEXT_LIMITS = {
    "kimi-code/k3-256k": 262144,
    "kimi-code/k3": 1048576,
    "kimi-code/kimi-for-coding": 262144,
    "kimi-code/kimi-for-coding-highspeed": 262144,
}
AGENT_CHOICES = {"codex", "kimi"}
INBOX_CONVERSATIONS = INBOX_DIR / "conversations"


def list_external_kimi_sessions(limit=100):
    """扫 ~/.kimi-code/sessions/*/session_*/state.json，返回时间倒序列表。

    每项 {session_id, title, updated_at(毫秒), cwd, archived}。
    容错：目录/文件缺失、JSON 损坏、字段缺失（title 缺→"(无标题)"）一律跳过该项，不抛异常。
    用 updated_at 倒序，截 limit。
    """
    sessions = []
    if not KIMI_EXTERNAL_SESSIONS_DIR.is_dir():
        return sessions
    for workdir in KIMI_EXTERNAL_SESSIONS_DIR.iterdir():
        if not workdir.is_dir():
            continue
        for session_dir in workdir.iterdir():
            if not session_dir.is_dir():
                continue
            state_path = session_dir / "state.json"
            if not state_path.is_file():
                continue
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            session_id = session_dir.name
            if not re.fullmatch(r"session_[a-z0-9-]+", session_id):
                continue
            updated_at = state.get("updatedAt")
            try:
                updated_at = int(updated_at)
            except (TypeError, ValueError):
                continue
            sessions.append({
                "session_id": session_id,
                "title": state.get("title") or "(无标题)",
                "updated_at": updated_at,
                "cwd": state.get("cwd", ""),
                "archived": bool(state.get("archived", False)),
            })
    sessions.sort(key=lambda item: item["updated_at"], reverse=True)
    return sessions[:limit]


def takeover_kimi_session(session_id):
    """把 CLI 侧会话复制到工作台 KIMI_CODE_HOME 并登记 index（幂等）。

    - 源：KIMI_EXTERNAL_SESSIONS_DIR 下 glob 找 */<session_id> 目录，找不到返回 False
    - 目标：KIMI_CODE_HOME/sessions/wd_takeover/<session_id>（已存在则跳过复制）
    - 登记：KIMI_CODE_HOME/session_index.jsonl 追加 {"sessionId","sessionDir","workDir":原 state.json 的 cwd}；已含该 sessionId 则跳过
    - 全程容错（OSError 等）返回 False；成功 True
    - 安全：只读源目录，不得修改/删除源；session_id 必须匹配 ^session_[a-z0-9-]+$ 防路径穿越
    """
    if not isinstance(session_id, str) or not re.fullmatch(r"session_[a-z0-9-]+", session_id):
        return False
    try:
        # 定位源会话目录
        source_dir = None
        source_state = None
        for workdir in KIMI_EXTERNAL_SESSIONS_DIR.glob("*"):
            candidate = workdir / session_id
            if candidate.is_dir() and (candidate / "state.json").is_file():
                source_dir = candidate
                source_state = json.loads((candidate / "state.json").read_text(encoding="utf-8"))
                break
        if source_dir is None:
            return False

        # 目标目录（只写工作台 home）
        target_dir = KIMI_SESSIONS_DIR / "wd_takeover" / session_id
        if not target_dir.exists():
            shutil.copytree(source_dir, target_dir)

        # 登记 index
        index_path = Path(KIMI_CODE_HOME) / "session_index.jsonl"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        existing = ""
        if index_path.exists():
            existing = index_path.read_text(encoding="utf-8")
        if session_id not in existing:
            entry = {
                "sessionId": session_id,
                "sessionDir": str(target_dir.resolve()),
                "workDir": source_state.get("cwd", ""),
            }
            with index_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False
WINDOWS_KEY = os.environ.get("CODEX_WEB_WINDOWS_KEY", str(Path.home() / ".ssh" / "windows_ed25519"))
WINDOWS_HOST = os.environ.get("CODEX_WEB_WINDOWS_HOST", "user@127.0.0.1")
WINDOWS_SSH_PORT = os.environ.get("CODEX_WEB_WINDOWS_SSH_PORT", "58231")
WINDOWS_TASKS = os.environ.get("CODEX_WEB_WINDOWS_TASKS", r"C:\Users\user\codex-web\tasks")
MAX_RUNTIME_SECONDS = int(os.environ.get("CODEX_WEB_MAX_RUNTIME_SECONDS", "21600"))
MAX_CONTEXT_CHARS = int(os.environ.get("CODEX_WEB_MAX_CONTEXT_CHARS", "24000"))
NO_OUTPUT_WARNING_SECONDS = int(os.environ.get("CODEX_WEB_NO_OUTPUT_WARNING_SECONDS", "90"))
# 懒收割宽限：kick 摘除挂起标记到 pid 落盘之间（含 spawn 线程交接）不判死，
# 防"queued 且无 pid"的新生轮被 get() 懒收割误杀。
REAPER_GRACE_SECONDS = int(os.environ.get("CODEX_WEB_REAPER_GRACE_SECONDS", "120"))
ACTIVE_STATUSES = {"queued", "running", "stopping"}
BLOCKING_STATUSES = ACTIVE_STATUSES | {"waiting_approval", "windows_executing"}
_METADATA_LOCK = threading.RLock()
_TERMINAL_CALLBACK = None

# WS 内容事件节流：段落级准实时（用户拍板 ≥1 秒/条，不做逐字流）。
# 进程内易失状态，终态时在 _emit_terminal 清理，重启自然清零。
_WS_STAGED_MIN_INTERVAL = float(os.environ.get("CODEX_WEB_WS_STAGED_MIN_INTERVAL", "1.0"))
_WS_STAGED_LAST = {}  # task_id -> 上次广播的 monotonic 时间戳

# 任务子进程统一由这个常驻单线程 spawn。
# bwrap --die-with-parent 的 PDEATHSIG 绑定「fork 它的线程」而非整个进程：
# 同步 FastAPI 端点跑在 anyio 线程池，worker 空闲约 10 秒会被回收退出，
# 线程一死，沙盒 kimi 立刻收 SIGKILL（手机端任务 11~25 秒被杀的根因）。
# 常驻线程与进程同寿命：worker 回收不再误杀任务；服务退出时该线程
# 随进程结束，--die-with-parent 依旧能兜底清理子进程。
_SPAWNER = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="task-spawner")


def set_terminal_callback(callback):
    global _TERMINAL_CALLBACK
    _TERMINAL_CALLBACK = callback


def _emit_terminal(task_id):
    # 排队兜底：任何终态相关事件都顺带检查是否有可续发的挂起轮（幂等）。
    try:
        _maybe_start_held()
    except Exception:
        pass
    callback = _TERMINAL_CALLBACK
    if callback is None:
        return
    with _METADATA_LOCK:
        metadata = _read_metadata(task_id)
        if metadata.get("status") in ACTIVE_STATUSES or metadata.get("terminal_event_emitted"):
            return
        metadata["terminal_event_emitted"] = True
        _write_metadata_locked(task_id, metadata)
    try:
        callback(dict(metadata))
    except Exception:
        pass
    _WS_STAGED_LAST.pop(task_id, None)  # 终态清理节流状态，防字典随任务数膨胀
    _ws_broadcast_status(metadata)
    if metadata.get("status") == "waiting_approval":
        # 审批卡自动出现：必须在状态翻转之后广播——前端收到即 reload，
        # 早于此（如 create_approval 时）reload 拿到的还是 running 页，没有卡片。
        try:
            request = windows_bridge.validated_approval(task_id)
            if request:
                push_threadsafe(manager.broadcast_approval(task_id, request))
        except Exception:
            pass




def _task_dir(task_id):
    return ROOT / task_id


def _process_identity(pid):
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text()
        # comm 字段可能含空格（如 kimi 进程改名为 "Kimi Code"），
        # 必须从最后一个右括号之后切分，否则字段错位、误判进程死亡。
        fields = raw[raw.rindex(")") + 1:].split()
        return fields[0], fields[19]
    except (OSError, ValueError, IndexError):
        return None, None


def _read_metadata(task_id):
    with _METADATA_LOCK:
        return json.loads((_task_dir(task_id) / "task.json").read_text(encoding="utf-8"))


def _write_metadata(task_id, metadata):
    with _METADATA_LOCK:
        (_task_dir(task_id) / "task.json").write_text(
            json.dumps(metadata, ensure_ascii=True), encoding="utf-8"
        )


def _write_metadata_locked(task_id, metadata):
    (_task_dir(task_id) / "task.json").write_text(
        json.dumps(metadata, ensure_ascii=True), encoding="utf-8"
    )


def _tail_lines(path, max_bytes=524288):
    """读取文件尾部最多 max_bytes 字节并按行返回（跳过首行不完整片段）。"""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    try:
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
                handle.readline()  # 丢弃可能被截断的首行
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()


def _parse_time(value, fallback=None):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return fallback or datetime.now(timezone.utc)


def _runtime_fields(metadata, output_path, now=None):
    now = now or datetime.now(timezone.utc)
    started = _parse_time(metadata.get("started") or metadata.get("created"), now)
    try:
        stat = output_path.stat()
    except OSError:
        stat = None
    last_output = datetime.fromtimestamp(stat.st_mtime, timezone.utc) if stat else started
    state, starttime = _process_identity(metadata.get("pid")) if metadata.get("pid") else (None, None)
    identity_matches = not metadata.get("process_starttime") or starttime == metadata.get("process_starttime")
    running = state not in {None, "Z"} and identity_matches
    output_bytes = stat.st_size if stat else 0
    no_output_seconds = max(0, int((now - last_output).total_seconds()))
    metadata.update({
        "elapsed_seconds": max(0, int((now - started).total_seconds())),
        "output_bytes": output_bytes,
        "last_output_at": last_output.isoformat(),
        "no_output_seconds": no_output_seconds,
        "process_state": state or "not_found",
        "output_warning": metadata.get("status") in ACTIVE_STATUSES and no_output_seconds >= NO_OUTPUT_WARNING_SECONDS,
    })
    return running, state, identity_matches


def _finish_from_process(task_id, returncode):
    directory = _task_dir(task_id)
    final_path = directory / "final.txt"
    with _METADATA_LOCK:
        try:
            metadata = json.loads((directory / "task.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return
        if metadata.get("agent") == "kimi" and not final_path.exists():
            _extract_kimi_final(directory)
        try:
            approval = windows_bridge.validated_approval(task_id)
        except (OSError, ValueError, json.JSONDecodeError):
            approval = None
            metadata["windows_error"] = "invalid_approval_request"
        if metadata.get("status") == "stopping":
            metadata["status"] = "cancelled"
        elif approval and approval.get("status") == "pending":
            metadata["status"] = "waiting_approval"
            metadata["windows_request_id"] = approval["id"]
        elif metadata.get("status") not in {"cancelled", "timed_out"}:
            if final_path.exists():
                # Kimi may be SIGKILLed mid-flush, leaving a truncated final.txt.
                # Treat <20 bytes as abnormal to avoid false "succeeded".
                # ACP 轮豁免：#1865 空转守卫已覆盖真空转，且 ACP 正文按完整
                # JSONL 行提取不存在截尾——短回复（"80"/"好的"）本来就短，不是异常。
                if (metadata.get("agent") == "kimi" and metadata.get("kimi_engine") != "acp"
                        and final_path.stat().st_size < 20):
                    metadata["status"] = "failed"
                else:
                    metadata["status"] = "succeeded"
            else:
                metadata["status"] = "failed"
        metadata["returncode"] = returncode
        if metadata["status"] == "waiting_approval":
            metadata.pop("finished", None)
        else:
            metadata["finished"] = datetime.now(timezone.utc).isoformat()
        metadata.pop("high_risk_authorized", None)
        metadata["authorization_scope"] = None
        # ACP 轮末收割上下文用量：失败/取消/正常结束均尝试，静默容错。
        if metadata.get("agent") == "kimi" and metadata.get("kimi_engine") == "acp":
            try:
                harvest_kimi_context_usage(metadata)
            except Exception:
                pass
        _runtime_fields(metadata, directory / "output.jsonl")
        _write_metadata_locked(task_id, metadata)
        # Cleanup user-level mcp.json once the task reaches a terminal state.
        # waiting_approval is not terminal: the conversation may continue after PIN approval.
        if metadata.get("status") != "waiting_approval":
            _cleanup_user_mcp_config()


def _reap_process(task_id, process):
    try:
        returncode = process.wait()
    except (OSError, ValueError):
        returncode = None
    _finish_from_process(task_id, returncode)
    # Fetch Windows' remote final message before deciding which terminal event
    # to report. WSL tasks already have their final message locally.
    try:
        get(task_id)
    finally:
        _emit_terminal(task_id)


def _trim_prompt(value, limit, from_end=False):
    value = str(value or "")
    if len(value) <= limit:
        return value
    marker = "\n[中间内容已省略]\n"
    keep = max(1, limit - len(marker))
    if from_end:
        return marker + value[-keep:]
    return value[:keep] + marker


def user_prompt(metadata):
    """Return the non-recursive user input stored for a conversation turn."""
    raw = metadata.get("user_prompt")
    if raw is not None:
        return str(raw)
    raw = str(metadata.get("prompt", ""))
    # Legacy turns stored the complete continuation prompt. Keep only its last
    # user addition when recovering context from those records.
    if metadata.get("parent_id") and "用户补充：\n" in raw:
        return raw.rsplit("用户补充：\n", 1)[-1]
    return raw


def continuation_prompt(task_id, new_prompt, purpose="continue"):
    """Build a bounded prompt without copying a prior task's full prompt."""
    chain = thread(task_id)
    heading = (
        "基于已有对话创建一个新的独立任务。以下是压缩后的必要历史；"
        "如果历史不完整，以本轮目标为准。"
        if purpose == "fork"
        else "继续当前对话。以下是压缩后的必要历史；如果历史不完整，以本轮补充为准。"
    )
    sections = [heading]
    for turn in chain:
        prompt = _trim_prompt(user_prompt(turn), 4200)
        if prompt:
            sections.append("用户：\n" + prompt)
        try:
            _, _, final = get(turn["id"])
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            final = ""
        if final:
            sections.append("Codex：\n" + _trim_prompt(final, 5200, from_end=True))
    label = "新目标" if purpose == "fork" else "本轮用户补充"
    sections.append(label + "：\n" + _trim_prompt(new_prompt, 12000))
    result = "\n\n".join(sections)
    if len(result) <= MAX_CONTEXT_CHARS:
        return result
    suffix = "\n\n[较早历史已省略，仅保留最近上下文]\n\n"
    keep = max(1, MAX_CONTEXT_CHARS - len(heading) - len(suffix))
    return heading + suffix + result[-keep:]


def create_draft():
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    INBOX_CONVERSATIONS.mkdir(mode=0o700, parents=True, exist_ok=True)
    task_id = secrets.token_hex(12)
    directory = _task_dir(task_id)
    directory.mkdir(mode=0o700)
    attachment_dir = INBOX_CONVERSATIONS / task_id
    attachment_dir.mkdir(mode=0o700)
    metadata = {
        "id": task_id, "target": "wsl", "prompt": "", "workdir": str(attachment_dir),
        "created": datetime.now(timezone.utc).isoformat(), "status": "draft",
        "project_id": None, "parent_id": None, "title": "新对话",
    }
    (directory / "task.json").write_text(json.dumps(metadata, ensure_ascii=True), encoding="utf-8")
    return metadata


def activate_draft(task_id, prompt, model=None, permission="workspace-write", reasoning="medium", agent=None, thinking=None, plan_mode=False):
    directory = _task_dir(task_id)
    metadata = _read_metadata(task_id)
    if metadata.get("status") != "draft" or metadata.get("project_id"):
        raise ValueError("对话草稿无效。")
    if agent not in AGENT_CHOICES:
        agent = _default_agent()
    metadata["prompt"] = prompt
    metadata["user_prompt"] = prompt
    metadata["title"] = prompt.replace("\n", " ")[:48]
    metadata["status"] = "queued"
    metadata["model"] = model
    metadata["permission"] = permission
    metadata["reasoning"] = reasoning
    metadata["agent"] = agent
    metadata["thinking"] = thinking
    metadata["plan_mode"] = plan_mode
    _write_metadata(task_id, metadata)
    return metadata


def get_draft(task_id):
    metadata = json.loads((_task_dir(task_id) / "task.json").read_text(encoding="utf-8"))
    if metadata.get("status") != "draft" or metadata.get("project_id"):
        raise ValueError("对话草稿无效。")
    attachment_dir = Path(metadata["workdir"]).resolve()
    if attachment_dir.parent != INBOX_CONVERSATIONS.resolve() or not attachment_dir.is_dir():
        raise ValueError("附件目录无效。")
    return metadata


def clean_empty_drafts(max_age_seconds=86400):
    if not ROOT.exists():
        return
    now = datetime.now(timezone.utc).timestamp()
    for directory in ROOT.iterdir():
        task_file = directory / "task.json"
        if not task_file.is_file():
            continue
        try:
            metadata = json.loads(task_file.read_text(encoding="utf-8"))
            if metadata.get("status") != "draft":
                continue
            created = datetime.fromisoformat(metadata["created"]).timestamp()
            attachment_dir = Path(metadata["workdir"])
            has_files = attachment_dir.is_dir() and any(attachment_dir.iterdir())
            if not has_files and now - created > max_age_seconds:
                attachment_dir.rmdir()
                for child in directory.iterdir():
                    child.unlink()
                directory.rmdir()
        except (OSError, ValueError, json.JSONDecodeError):
            continue


def recover_interrupted_windows_executions():
    """Never replay a Windows write when the web service restarted mid-action.

    For local service-restart actions, the restart itself is the execution;
    mark them executed and continue the conversation.
    """
    if not ROOT.exists():
        return
    for directory in ROOT.iterdir():
        task_file = directory / "task.json"
        if not task_file.is_file():
            continue
        try:
            metadata = json.loads(task_file.read_text(encoding="utf-8"))
            if metadata.get("status") != "windows_executing":
                continue
            task_id = metadata["id"]
            try:
                request = windows_bridge.read_approval(task_id)
            except (OSError, ValueError):
                request = None
            if (
                request
                and request.get("source") == "local"
                and request.get("action") == "restart_codex_web"
                and request.get("status") == "executing"
            ):
                request_id = request["id"]
                request["status"] = "executed"
                request["executed"] = datetime.now(timezone.utc).isoformat()
                request["result"] = "服务已重启。"
                windows_bridge.write_approval(task_id, request)
                windows_bridge.append_audit(task_id, "execution_finished", {
                    "request_id": request_id, "status": "executed", "result": request["result"],
                })
                metadata["status"] = "succeeded"
                metadata["finished"] = datetime.now(timezone.utc).isoformat()
                _write_metadata(task_id, metadata)
                _cleanup_user_mcp_config()
                try:
                    _continue_after_windows(task_id, request)
                except Exception:
                    pass
            else:
                metadata["status"] = "windows_execution_uncertain"
                metadata["finished"] = datetime.now(timezone.utc).isoformat()
                metadata["windows_error"] = "service_restarted_during_execution"
                _write_metadata(task_id, metadata)
        except (OSError, ValueError, json.JSONDecodeError, KeyError):
            continue


def _default_agent():
    override = os.environ.get("WORKBENCH_AGENT", "").strip().lower()
    if override in AGENT_CHOICES:
        return override
    try:
        value = AGENT_STATE_FILE.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return "kimi"
    return value if value in AGENT_CHOICES else "kimi"


def _audit(event, detail=""):
    """Append a lightweight audit record from the task layer (no request IP)."""
    try:
        STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        record = {
            "time": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "detail": detail,
        }
        with AUDIT_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=True) + "\n")
        AUDIT_FILE.chmod(0o600)
    except OSError:
        pass


def _bwrap_available():
    return shutil.which("bwrap") is not None


def _bwrap_argv(workdir, permission):
    """Return the bwrap argv prefix for a sandboxed kimi spawn.

    The sandbox exposes /usr, /lib, /lib64, /bin, /etc as read-only,
    mounts the kimi binary read-only, provides a read-write KIMI_CODE_HOME
    for token refresh, and binds the task workdir read-only or read-write.
    The projects root (projects.ROOT) is mounted read-only so a task can
    read material from sibling projects; a workdir inside it keeps its own
    read-write binding because the later workdir bind stacks on top.
    ~/.ssh and the interactive ~/.kimi-code credentials are not mounted.

    The user's home directory is replaced by a tmpfs and then remounted
    read-only so that writes outside the explicit workdir / KIMI_CODE_HOME
    bindings fail.
    """
    workdir = str(workdir)
    workdir_ro = permission == "read-only"
    home_dir = str(Path.home())
    argv = [
        "bwrap",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/lib", "/lib",
        "--ro-bind", "/lib64", "/lib64",
        "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/etc", "/etc",
        # WSL resolves DNS via /mnt/wsl/resolv.conf; keep it reachable.
        "--ro-bind", "/mnt/wsl/resolv.conf", "/mnt/wsl/resolv.conf",
        # Replace the home directory with an empty tmpfs, then expose only the
        # paths the sandboxed kimi is allowed to use. Remount it read-only
        # afterwards so writes to any other location under it fail.
        "--tmpfs", home_dir,
        "--dir", str(Path.home() / ".kimi-code" / "bin"),
        "--ro-bind", KIMI_COMMAND, KIMI_COMMAND,
        "--bind", KIMI_CODE_HOME, KIMI_CODE_HOME,
    ]
    # 跨项目只读：整个项目根只读挂进沙盒，任务可直接读兄弟项目的素材。
    # 必须排在 workdir 挂载之前：workdir 在项目根内时，后挂的绑定叠在
    # 只读绑定之上，当前项目仍按权限保持可写。目录不存在时跳过，
    # 不能让 spawn 因项目根缺失而整体失败。
    projects_root = str(projects.ROOT)
    if os.path.isdir(projects_root):
        argv.extend(["--ro-bind", projects_root, projects_root])
    # 共享工具目录只读：~/Tools 存放跨项目共用的工具/venv/模型/浏览器
    # （filebrowser 已在内）。必须只读——沙盒内 AI 不可写共享工具，防单
    # 项目被提示词注入后污染全部项目（供应链）；安装/更新走用户终端或
    # 完全访问档。目录不存在时跳过，与项目根同理。
    tools_root = os.environ.get("CODEX_WEB_TOOLS_DIR", str(Path.home() / "Tools"))
    if os.path.isdir(tools_root):
        argv.extend(["--ro-bind", tools_root, tools_root])
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


def _user_mcp_path():
    return Path(KIMI_CODE_HOME) / "mcp.json"


def _write_user_mcp_config(task_id):
    """Write the user-level mcp.json for the sandboxed kimi HTTP bridge.

    kimi print mode does not load project-level .kimi-code/mcp.json, so we
    use the user-level config inside KIMI_CODE_HOME instead. Concurrent kimi
    tasks will overwrite each other's URL, but the task_id in the URL makes
    misrouting visible rather than silently crossing permission boundaries.
    """
    config = {
        "mcpServers": {
            "codex-web-bridge": {
                "url": f"http://127.0.0.1:{os.environ.get('CODEX_WEB_PORT', '18080')}/bridge/mcp/{task_id}"
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


def _cleanup_user_mcp_config():
    """Remove the user-level mcp.json after the task finishes."""
    path = _user_mcp_path()
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _thread_kimi_session(metadata):
    """沿 parent 链找最近的 kimi ACP 会话 ID（追问轮 resume 真历史的钥匙）。

    当前任务已预写 kimi_session_id 时（如外部会话接管）直接返回，不向上查找。
    """
    if metadata.get("agent") == "kimi" and metadata.get("kimi_session_id"):
        return metadata["kimi_session_id"]
    current_id = metadata.get("parent_id")
    seen = set()
    while current_id and current_id not in seen:
        seen.add(current_id)
        try:
            parent = _read_metadata(current_id)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return None
        if parent.get("agent") == "kimi" and parent.get("kimi_session_id"):
            return parent["kimi_session_id"]
        current_id = parent.get("parent_id")
    return None


def _kimi_session_busy(session_id, exclude_task_id=None):
    """同一 ACP 会话是否还有活动轮（queued/running/stopping）。

    两进程并发 resume 同一会话会互相踩踏（ACP 同会话同刻只允许一个 turn），
    命中时调用方应回退 legacy spawn 老路。
    exclude_task_id：排除调用方自身——重 start/外部会话接管（预写 id）场景下
    本任务 metadata 已带该 session id 且已 running，不算被他人踩踏。
    """
    if not ROOT.exists():
        return False
    for directory in ROOT.iterdir():
        task_file = directory / "task.json"
        if not task_file.is_file():
            continue
        if exclude_task_id and directory.name == exclude_task_id:
            continue
        try:
            meta = json.loads(task_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if meta.get("kimi_session_id") == session_id and meta.get("status") in ACTIVE_STATUSES:
            return True
    return False


def _kimi_assistant_text(event):
    """Return the user-facing text from a Kimi assistant event (text-only)."""
    if event.get("role") != "assistant":
        return None
    content = event.get("content")
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        text = "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()
        return text or None
    return None


def _kimi_staged_text(event):
    """Return a user-visible staged message from a Kimi assistant event.

    Prefers the public ``text`` content.  When only ``think`` content is
    available (common while Kimi is reasoning between tool calls), expose a
    short thinking snippet so the UI can show progress updates similar to
    Codex ``agent_message`` events.
    """
    if event.get("role") != "assistant":
        return None
    content = event.get("content")
    text_parts = []
    think_parts = []
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                text_parts.append(part.get("text", ""))
            elif part.get("type") == "think":
                think_parts.append(part.get("think", ""))
    elif isinstance(content, str):
        text_parts.append(content)
    text = "".join(text_parts).strip()
    if text:
        return text
    think = "".join(think_parts).strip()
    if think:
        snippet = think.replace("\n", " ").strip()
        if len(snippet) > 200:
            snippet = snippet[:200] + "…"
        return "🤔 " + snippet
    return None


def _extract_kimi_final(directory):
    output_path = directory / "output.jsonl"
    if not output_path.exists():
        return
    segments = []
    try:
        with output_path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                text = _kimi_assistant_text(event)
                # 一轮里"说话→调工具→再说话"会产生多个增量文本段
                # （ACP 与 legacy stream-json 同构），只留末段会把
                # 工具调用之前的正文整段丢给用户（截尾 BUG），按序拼接。
                if text and (not segments or segments[-1] != text):
                    segments.append(text)
    except OSError:
        return
    if segments:
        (directory / "final.txt").write_text("\n\n".join(segments) + "\n", encoding="utf-8")


def create(target, prompt, workdir, project_id=None, parent_id=None, title=None, model=None, permission="workspace-write", reasoning="medium", user_prompt=None, agent=None, held=False, thinking=None, plan_mode=False):
    return _create(target, prompt, workdir, project_id, parent_id, title, model, permission, reasoning, user_prompt, agent, held, thinking, plan_mode)


def _create(target, prompt, workdir, project_id=None, parent_id=None, title=None, model=None, permission="workspace-write", reasoning="medium", user_prompt=None, agent=None, held=False, thinking=None, plan_mode=False):
    if len(prompt) > MAX_CONTEXT_CHARS:
        raise ValueError("任务上下文超过安全上限，请改用新对话并附简短摘要。")
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    task_id = secrets.token_hex(12)
    directory = _task_dir(task_id)
    directory.mkdir(mode=0o700)
    effective_user_prompt = prompt if user_prompt is None else user_prompt
    if target == "windows":
        agent = "codex"
    elif agent not in AGENT_CHOICES:
        agent = None
    if agent is None and parent_id:
        try:
            parent_agent = _read_metadata(parent_id).get("agent")
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            parent_agent = None
        if parent_agent in AGENT_CHOICES:
            agent = parent_agent
    if agent is None:
        agent = _default_agent()
    metadata = {
        "id": task_id,
        "target": target,
        "prompt": prompt,
        "workdir": workdir,
        "created": datetime.now(timezone.utc).isoformat(),
        "status": "queued",
        "project_id": project_id,
        "parent_id": parent_id,
        "title": title or effective_user_prompt.replace("\n", " ")[:48],
        "user_prompt": effective_user_prompt,
        "model": model,
        "permission": permission,
        "reasoning": reasoning,
        "agent": agent,
        "thinking": thinking,
        "plan_mode": plan_mode,
    }
    if held:
        # 排队轮：同对话还有活动轮时先挂起不启动，
        # 活动轮到终态后由 _maybe_start_held 自动续发。
        metadata["held"] = True
    _write_metadata(task_id, metadata)
    return metadata


# 网页端（含手机）kimi 任务的环境说明，随 prompt 注入：
# 避免模型误用仅用户交互终端可用的通道（如直接 SSH 到 Windows）。
_KIMI_ENV_NOTE = (
    "[运行环境] 你是网页工作台发起的任务，运行在沙盒中：不能读写工作目录之外的文件，"
    "也没有直接 SSH 到 Windows 的密钥（该通道仅用户终端可用）。"
    "Windows 操作请使用 mcp__codex-web-bridge__windows_inspect（只读检查，直接执行）"
    "或 mcp__codex-web-bridge__windows_execute（写操作，将请用户 PIN 审批后执行）；"
    "重启本工作台等本机操作用 mcp__codex-web-bridge__local_execute（同样 PIN 审批）。\n"
    "[共享工具] " + os.environ.get("CODEX_WEB_TOOLS_DIR", str(Path.home() / "Tools")) + " 只读可用（沙盒内不可写，写入会报 Read-only file system）："
    "无头浏览器（playwright+chromium，前端预览/截图）、ffmpeg/ffprobe、"
    "共享 python（pandas/openpyxl/Pillow/requests），用法与环境变量见其 README.md。"
    "安装新工具只能由用户终端或完全访问档任务执行。\n"
    "[交流规则] 思考过程与对用户的输出一律用中文；重要结论与答复必须放在正文输出，"
    "不要只写在思考过程里（思考内容不会展示给用户）。\n\n"
)


def start(task_id):
    directory = _task_dir(task_id)
    metadata = json.loads((directory / "task.json").read_text(encoding="utf-8"))
    metadata.pop("held", None)  # 启动即摘除挂起标记（kick 已摘，此处防御）
    output = (directory / "output.jsonl").open("ab")
    final = directory / "final.txt"
    env = None
    cwd = None
    if metadata["target"] == "windows":
        payload = base64.b64encode(json.dumps({
            "workdir": metadata["workdir"], "prompt": metadata["prompt"],
            "model": metadata.get("model"), "reasoning": metadata.get("reasoning", "medium"),
            "permission": metadata.get("permission", "workspace-write"),
        }, ensure_ascii=False).encode("utf-8")).decode("ascii")
        script = (
            "$payload=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('%s'))|ConvertFrom-Json;"
            "$dir='%s\\%s';New-Item -ItemType Directory -Force -Path $dir|Out-Null;"
            "$args=@('exec','--json','--skip-git-repo-check','-s',$payload.permission,'-C',$payload.workdir,"
            "'-c',('model_reasoning_effort=\"'+$payload.reasoning+'\"'));"
            "if($payload.model){$args+=@('-m',$payload.model)};"
            "$args+=@('--output-last-message',(Join-Path $dir 'final.txt'),$payload.prompt);"
            "& codex @args"
        ) % (payload, WINDOWS_TASKS, task_id)
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        command = [
            "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-i", WINDOWS_KEY, "-p", WINDOWS_SSH_PORT, WINDOWS_HOST,
            "powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded,
        ]
        metadata["windows_final"] = WINDOWS_TASKS + "\\" + task_id + "\\final.txt"
    elif metadata.get("agent") == "kimi":
        permission = metadata.get("permission", "workspace-write")
        sandboxed = permission != "danger-full-access" and _bwrap_available()
        # 未显式选模型时回落到 kimi 默认模型（当前 k3-256k）：
        # 存量任务（metadata 无 model）的追问/分叉随之统一切换。
        kimi_model = metadata.get("model") or kimi_default_model()
        kimi_thinking = metadata.get("thinking") or kimi_default_thinking()
        kimi_plan_mode = bool(metadata.get("plan_mode", False))
        cwd = metadata["workdir"]
        # kimi 输出到文件时 stdout 块缓冲，事件直到进程退出才落盘；
        # 强制无缓冲，过程区才能看到实时流。
        # 与终端交互式 kimi 分家，避免 web spawn（print 模式自动批准）
        # 加载本机 MCP 配置。
        env = dict(
            os.environ,
            PYTHONUNBUFFERED="1",
            KIMI_CODE_HOME=KIMI_CODE_HOME,
        )
        acp_session_id = None
        use_acp = kimi_engine() == "acp"
        if use_acp:
            acp_session_id = _thread_kimi_session(metadata)
            if acp_session_id and _kimi_session_busy(acp_session_id, exclude_task_id=task_id):
                # 同会话还有活动轮（连点追问等）：两进程并发 resume 同一会话
                # 会互相踩踏，本轮回退 legacy spawn 老路并记审计。
                _audit("acp_conflict_fallback", {
                    "task_id": task_id, "session_id": acp_session_id})
                use_acp = False
                acp_session_id = None
        if use_acp:
            # ACP 路线：追问轮 resume 真实全量历史，只发用户新输入；
            # 新会话发完整 prompt（根任务=用户原文；分叉=压缩上下文，语义不变）。
            base_prompt = user_prompt(metadata) if acp_session_id else metadata["prompt"]
            acp_prompt = _KIMI_ENV_NOTE + base_prompt if sandboxed else base_prompt
            command = [KIMI_COMMAND, "acp"]
            if sandboxed:
                _write_user_mcp_config(task_id)
                command = _bwrap_argv(metadata["workdir"], permission) + command
            elif permission != "danger-full-access":
                _audit("sandbox_unavailable", {"task_id": task_id, "permission": permission})
            output.close()  # 适配层自行打开 output.jsonl 写合成事件
            try:
                process, acp_session_id = _SPAWNER.submit(
                    kimi_acp.start_turn,
                    task_id, command, env, cwd,
                    str(directory / "output.jsonl"), acp_prompt,
                    acp_session_id, kimi_model, kimi_default_model(),
                    kimi_thinking, kimi_plan_mode, _audit,
                    _ws_broadcast_staged,
                ).result()
            except Exception as error:
                # 握手/建会话失败（401、协议不符等红线）：诚实判败，不静默回退。
                metadata["status"] = "failed"
                metadata["finished"] = datetime.now(timezone.utc).isoformat()
                metadata["start_error"] = type(error).__name__ + ": " + str(error)[:300]
                _write_metadata(task_id, metadata)
                _audit("acp_start_failed", {"task_id": task_id, "error": str(error)[:300]})
                _cleanup_user_mcp_config()
                return metadata
            metadata["kimi_engine"] = "acp"
            metadata["kimi_session_id"] = acp_session_id
            metadata["status"] = "running"
            metadata["pid"] = process.pid
            _, metadata["process_starttime"] = _process_identity(process.pid)
            metadata["started"] = datetime.now(timezone.utc).isoformat()
            metadata["last_output_at"] = metadata.get("started")
            _write_metadata(task_id, metadata)
            threading.Thread(target=_reap_process, args=(task_id, process), daemon=True).start()
            return metadata
        # legacy spawn 路线（默认；一键回退落点）。
        # 仅沙盒模式注入环境说明：此时桥接工具已注入且无 SSH 密钥；
        # 危险全权模式下模型本就可直接访问，说明内容与事实不符。
        prompt = _KIMI_ENV_NOTE + metadata["prompt"] if sandboxed else metadata["prompt"]
        command = [
            KIMI_COMMAND, "--output-format", "stream-json", "-p", prompt,
        ]
        if kimi_model:
            command.extend(["-m", kimi_model])
        if sandboxed:
            _write_user_mcp_config(task_id)
            command = _bwrap_argv(metadata["workdir"], permission) + command
        elif permission != "danger-full-access":
            # Sandbox requested but bwrap not installed: fall back to plain spawn
            # and audit the degraded execution.
            _audit("sandbox_unavailable", {"task_id": task_id, "permission": permission})
    else:
        bridge_args = json.dumps([
            str(Path(__file__).with_name("windows_bridge.py")), "--task-id", task_id,
        ])
        command = [
            "codex", "exec", "--json", "--skip-git-repo-check",
            "-c", "approval_policy=\"never\"",
            "-c", "mcp_servers.windows_bridge.command=" + json.dumps(sys.executable),
            "-c", "mcp_servers.windows_bridge.args=" + bridge_args,
            "-c", "mcp_servers.windows_bridge.required=true",
            "-c", "mcp_servers.windows_bridge.enabled_tools=[\"windows_inspect\",\"windows_execute\"]",
            "-c", "mcp_servers.windows_bridge.default_tools_approval_mode=\"approve\"",
            "-c", "mcp_servers.windows_bridge.tools.windows_execute.approval_mode=\"approve\"",
            "-s", metadata.get("permission", "workspace-write"), "-C", metadata["workdir"],
            "--output-last-message", str(final), metadata["prompt"],
        ]
        if metadata.get("model"):
            command[2:2] = ["-m", metadata["model"]]
        if metadata.get("reasoning"):
            command[2:2] = ["-c", "model_reasoning_effort=\"" + metadata["reasoning"] + "\""]
    process = _SPAWNER.submit(
        subprocess.Popen,
        command,
        stdout=output,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
        cwd=cwd,
    ).result()
    output.close()
    metadata["status"] = "running"
    metadata["pid"] = process.pid
    _, metadata["process_starttime"] = _process_identity(process.pid)
    metadata["started"] = datetime.now(timezone.utc).isoformat()
    metadata["last_output_at"] = metadata.get("started")
    _write_metadata(task_id, metadata)
    threading.Thread(target=_reap_process, args=(task_id, process), daemon=True).start()
    return metadata


def get(task_id):
    directory = _task_dir(task_id)
    metadata = _read_metadata(task_id)
    output_path = directory / "output.jsonl"
    final_path = directory / "final.txt"
    output = output_path.read_text(encoding="utf-8", errors="replace")[-16000:] if output_path.exists() else ""
    transitioned = False
    if metadata.get("status") in ACTIVE_STATUSES and not metadata.get("held"):
        # Re-read and transition under the same lock used by cancel() and the
        # reaper so a status poll cannot overwrite a newer terminal state.
        with _METADATA_LOCK:
            metadata = json.loads((directory / "task.json").read_text(encoding="utf-8"))
            # 挂起轮（held）设计上无进程，不收割，等活动轮终态后续发
            if metadata.get("status") in ACTIVE_STATUSES and not metadata.get("held"):
                running, state, identity_matches = _runtime_fields(metadata, output_path)
                started = datetime.fromisoformat(metadata.get("started", metadata["created"]))
                elapsed = (datetime.now(timezone.utc) - started).total_seconds()
                if running and elapsed > MAX_RUNTIME_SECONDS and metadata.get("status") != "stopping":
                    try:
                        os.killpg(metadata["pid"], signal.SIGTERM)
                    except (ProcessLookupError, PermissionError):
                        pass
                    metadata["status"] = "timed_out"
                    metadata["finished"] = datetime.now(timezone.utc).isoformat()
                    metadata.pop("high_risk_authorized", None)
                    metadata["authorization_scope"] = None
                    transitioned = True
                elif not running and (metadata.get("status") == "stopping" or elapsed >= REAPER_GRACE_SECONDS):
                    if metadata.get("agent") == "kimi" and metadata.get("status") != "stopping" and not final_path.exists():
                        _extract_kimi_final(directory)
                    metadata["status"] = "cancelled" if metadata.get("status") == "stopping" else ("succeeded" if final_path.exists() else "failed")
                    metadata["finished"] = datetime.now(timezone.utc).isoformat()
                    metadata.pop("high_risk_authorized", None)
                    metadata["authorization_scope"] = None
                    transitioned = True
                if transitioned:
                    # 终态任务不再保留"暂无新输出"警告（此前在状态翻转为终态
                    # 之前就算好警告并一起落盘）。懒恢复（进程非正常收割、
                    # returncode 缺失）而判成功时打 recovered 诚实标记。
                    metadata["output_warning"] = False
                    if metadata["status"] == "succeeded" and "returncode" not in metadata:
                        metadata["recovered"] = True
                    _write_metadata_locked(task_id, metadata)
    if metadata["target"] == "windows" and metadata.get("status") in {"finished", "succeeded", "failed"} and not final_path.exists():
        remote_final = metadata["windows_final"].replace("\\", "/")
        subprocess.run(
            ["scp", "-q", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
             "-i", WINDOWS_KEY, "-P", WINDOWS_SSH_PORT,
             WINDOWS_HOST + ":" + remote_final, str(final_path)],
            check=False, timeout=10,
        )
        if final_path.exists() and metadata.get("status") in {"finished", "failed"}:
            metadata["status"] = "succeeded"
            (directory / "task.json").write_text(json.dumps(metadata, ensure_ascii=True), encoding="utf-8")
            transitioned = True
    final = final_path.read_text(encoding="utf-8", errors="replace")[-8000:] if final_path.exists() else ""
    if transitioned:
        _emit_terminal(task_id)
    return metadata, output, final


def status_snapshot(task_id, include_details=False):
    metadata, output, final = get(task_id)
    if metadata.get("status") not in ACTIVE_STATUSES and not metadata.get("elapsed_seconds"):
        started = _parse_time(metadata.get("started") or metadata.get("created"))
        finished = _parse_time(metadata.get("finished"), datetime.now(timezone.utc))
        metadata["elapsed_seconds"] = max(0, int((finished - started).total_seconds()))
    snapshot = {
        "id": metadata.get("id"),
        "status": metadata.get("status"),
        "elapsed_seconds": metadata.get("elapsed_seconds", 0),
        "no_output_seconds": metadata.get("no_output_seconds", 0),
        "output_warning": bool(metadata.get("output_warning")) and metadata.get("status") in ACTIVE_STATUSES,
        "recovered": bool(metadata.get("recovered")),
        "output_bytes": metadata.get("output_bytes", 0),
        "last_output_at": metadata.get("last_output_at"),
        "agent_message_count": len(task_agent_messages(task_id)),
        "windows_request": approval_snapshot(task_id),
    }
    if include_details:
        snapshot.update({"summary": summarize(output), "output_tail": output})
    return snapshot


def _ws_broadcast_status(metadata):
    """Push a task.status event to all connected WebSocket clients."""
    try:
        snapshot = status_snapshot(metadata["id"])
        push_threadsafe(
            manager.broadcast_task_status(
                metadata["id"], metadata.get("status", ""),
                label=snapshot.get("label", ""),
                elapsed_seconds=snapshot.get("elapsed_seconds", 0),
                no_output_seconds=snapshot.get("no_output_seconds", 0),
                output_warning=snapshot.get("output_warning", False),
            )
        )
    except Exception:
        pass


def _ws_broadcast_staged(task_id, event):
    """kimi ACP 段落落盘回调（event_fn）：节流后广播最新阶段性回复。

    段落级准实时（≥1 秒/条，用户拍板不做逐字流）；被节流丢弃的段落由
    后续段落或终态整页渲染收敛，气泡内容始终取最新一条。
    """
    text = _kimi_staged_text(event)
    if not text:
        return
    now = time.monotonic()
    if now - _WS_STAGED_LAST.get(task_id, 0.0) < _WS_STAGED_MIN_INTERVAL:
        return
    _WS_STAGED_LAST[task_id] = now
    try:
        push_threadsafe(manager.broadcast_task_output(task_id, staged=text))
    except Exception:
        pass


def cancel(task_id):
    directory = _task_dir(task_id)
    with _METADATA_LOCK:
        metadata = json.loads((directory / "task.json").read_text(encoding="utf-8"))
        if metadata.get("status") not in ACTIVE_STATUSES:
            return metadata
        state, starttime = _process_identity(metadata.get("pid")) if metadata.get("pid") else (None, None)
        identity_matches = not metadata.get("process_starttime") or starttime == metadata.get("process_starttime")
        can_stop = identity_matches and state not in {None, "Z"}
        if metadata.get("kimi_engine") == "acp" and can_stop:
            # ACP 路线先走 session/cancel 优雅急停（探针实测 0.01s 停流），
            # 2.5s 内进程不退再落回下面的 SIGTERM 硬杀。
            try:
                if kimi_acp.request_cancel(task_id):
                    deadline = time.time() + 2.5
                    while time.time() < deadline:
                        graceful_state, _ = _process_identity(metadata.get("pid"))
                        if graceful_state in {None, "Z"}:
                            break
                        time.sleep(0.1)
                    state, starttime = _process_identity(metadata.get("pid"))
                    identity_matches = not metadata.get("process_starttime") or starttime == metadata.get("process_starttime")
                    can_stop = identity_matches and state not in {None, "Z"}
            except Exception:
                pass
        if can_stop:
            try:
                os.killpg(metadata["pid"], signal.SIGTERM)
            except ProcessLookupError:
                can_stop = False
        metadata["status"] = "stopping" if can_stop else "cancelled"
        if metadata["status"] == "cancelled":
            metadata["finished"] = datetime.now(timezone.utc).isoformat()
            metadata.pop("high_risk_authorized", None)
            metadata["authorization_scope"] = None
        _runtime_fields(metadata, directory / "output.jsonl")
        _write_metadata_locked(task_id, metadata)
    if metadata["status"] == "cancelled":
        _emit_terminal(task_id)
    return metadata


def approval_snapshot(task_id):
    try:
        request = windows_bridge.validated_approval(task_id)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not request:
        return None
    return {
        key: request.get(key)
        for key in ("id", "status", "source", "action", "summary", "reason", "created", "approved", "executed")
    }


def approval_detail(task_id):
    return windows_bridge.validated_approval(task_id)


def _windows_followup_prompt(request):
    source = request.get("source", "windows")
    source_label = "本地操作" if source == "local" else "Windows 操作"
    if request["status"] == "rejected":
        return (
            "用户拒绝了 " + source_label + " 请求：" + request["summary"] + "。"
            "不要执行该操作，也不要用其他通道绕过审批。请说明结果并在可能时寻找替代方案。"
        )
    result = request.get("result", "")
    state = "已执行成功" if request["status"] == "executed" else "执行失败"
    if source == "local":
        if request["action"] == "restart_codex_web" and request["status"] == "executed":
            return (
                "服务已重启。\n\n"
                "操作：" + request["summary"] + "\n"
                "返回：\n" + _trim_prompt(result, 8000, from_end=True) + "\n"
                "请基于该结果继续完成用户原来的目标；不要重复执行同一操作。"
            )
        return (
            "用户已批准本地操作，工作台宿主" + state + "。\n"
            "操作：" + request["summary"] + "\n"
            "返回：\n" + _trim_prompt(result, 8000, from_end=True) + "\n"
            "请基于该结果继续完成用户原来的目标；不要重复执行同一操作。"
        )
    return (
        "用户已批准 Windows 操作，工作台宿主" + state + "。\n"
        "操作：" + request["summary"] + "\n"
        "Windows 返回：\n" + _trim_prompt(result, 8000, from_end=True) + "\n"
        "请基于该结果继续完成用户原来的目标；不要重复执行同一操作。"
    )


def _continue_after_windows(task_id, request):
    with _METADATA_LOCK:
        metadata = _read_metadata(task_id)
        request = windows_bridge.validated_approval(task_id)
        if request.get("followup_task_id"):
            return request["followup_task_id"]
        prompt = _windows_followup_prompt(request)
        combined = continuation_prompt(task_id, prompt)
        followup = _create(
            "wsl", combined, metadata["workdir"], metadata.get("project_id"), task_id,
            metadata.get("title"), metadata.get("model"), metadata.get("permission", "workspace-write"),
            metadata.get("reasoning", "medium"), prompt,
            thinking=metadata.get("thinking"),
            plan_mode=bool(metadata.get("plan_mode", False)),
        )
        request["followup_task_id"] = followup["id"]
        windows_bridge.write_approval(task_id, request)
        metadata["status"] = "succeeded"
        metadata["finished"] = datetime.now(timezone.utc).isoformat()
        metadata["windows_followup_id"] = followup["id"]
        _write_metadata_locked(task_id, metadata)
    try:
        start(followup["id"])
    except Exception as error:
        with _METADATA_LOCK:
            failed = _read_metadata(followup["id"])
            failed["status"] = "failed"
            failed["finished"] = datetime.now(timezone.utc).isoformat()
            failed["start_error"] = type(error).__name__
            _write_metadata_locked(followup["id"], failed)
    return followup["id"]


def _execute_windows_approval(task_id, request_id):
    try:
        request = windows_bridge.execute_approval(task_id, request_id)
        if request.get("source") == "local":
            # Local actions such as restart_codex_web intentionally leave the
            # task as windows_executing while a daemon thread kills uvicorn.
            # Recovery after systemd restart completes the followup.
            return
        _continue_after_windows(task_id, request)
    except Exception as error:
        import traceback as _traceback
        _audit("windows_execution_failed", {
            "task_id": task_id,
            "request_id": request_id,
            "error": type(error).__name__,
            "message": str(error),
            "traceback": _traceback.format_exc()[-2000:],
        })
        with _METADATA_LOCK:
            metadata = _read_metadata(task_id)
            metadata["status"] = "failed"
            metadata["finished"] = datetime.now(timezone.utc).isoformat()
            metadata["windows_error"] = type(error).__name__
            _write_metadata_locked(task_id, metadata)
        _emit_terminal(task_id)


def approve_windows(task_id, request_id):
    with _METADATA_LOCK:
        metadata = _read_metadata(task_id)
        request = windows_bridge.validated_approval(task_id)
        if metadata.get("status") != "waiting_approval":
            raise ValueError("任务当前不在等待 Windows 授权。")
        if not request or request.get("id") != request_id or request.get("status") != "pending":
            raise ValueError("Windows 请求不存在或已经处理。")
        metadata["status"] = "windows_executing"
        metadata["windows_execution_started"] = datetime.now(timezone.utc).isoformat()
        _write_metadata_locked(task_id, metadata)
    threading.Thread(target=_execute_windows_approval, args=(task_id, request_id), daemon=True).start()
    return metadata


def reject_windows(task_id, request_id):
    with _METADATA_LOCK:
        metadata = _read_metadata(task_id)
        if metadata.get("status") != "waiting_approval":
            raise ValueError("任务当前不在等待 Windows 授权。")
        request = windows_bridge.reject_approval(task_id, request_id)
    return _continue_after_windows(task_id, request)


def summarize(events):
    lines = []
    for line in events.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type", "")
        if kind == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                content = item.get("text", "").strip()
                if content:
                    lines.append(content)
            elif item.get("type") == "command_execution":
                lines.append("命令完成：" + item.get("command", "")[:240])
        elif kind == "error":
            lines.append("错误：" + str(event.get("message", ""))[:400])
        elif event.get("role") == "assistant":
            text = _kimi_assistant_text(event)
            if text:
                lines.append(text)
    return "\n\n".join(lines[-20:])


def agent_messages(events):
    """Return complete assistant messages already present in the event stream."""
    messages = []
    for line in events.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message" and item.get("text", "").strip():
                messages.append(item["text"].strip())
        elif event.get("role") == "assistant":
            text = _kimi_staged_text(event)
            if text:
                messages.append(text)
    return messages


def task_agent_messages(task_id):
    """Scan the complete event log so later tool output cannot hide a reply."""
    path = _task_dir(task_id) / "output.jsonl"
    if not path.exists():
        return []
    messages = []
    with path.open(encoding="utf-8", errors="replace") as events:
        for line in events:
            messages.extend(agent_messages(line))
    return messages


def actual_config(events, target="wsl"):
    thread_id = None
    for line in events.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
            break
    if not thread_id:
        return None
    if target != "wsl":
        return {"thread_id": thread_id, "model": None, "reasoning": None}
    matches = list(CODEX_SESSIONS.glob("**/*" + thread_id + ".jsonl"))
    if not matches:
        return {"thread_id": thread_id, "model": None, "reasoning": None}
    actual = {"thread_id": thread_id, "model": None, "reasoning": None}
    try:
        with matches[0].open(encoding="utf-8", errors="replace") as session:
            for line in session:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") != "turn_context":
                    continue
                payload = event.get("payload", {})
                actual["model"] = payload.get("model") or actual["model"]
                actual["reasoning"] = payload.get("effort") or actual["reasoning"]
    except OSError:
        pass
    return actual


def _kimi_wire_path(session_id):
    """按 session_id 定位 ACP 会话 wire.jsonl。

    生产目录结构：<KIMI_CODE_HOME>/sessions/<workspace>/<session_id>/agents/main/wire.jsonl
    注意 kimi_session_id 自带 "session_" 前缀，目录名=完整 id，pattern 不得再加前缀。
    """
    if not session_id or not KIMI_SESSIONS_DIR.exists():
        return None
    for candidate in KIMI_SESSIONS_DIR.glob(f"*/{session_id}/agents/main/wire.jsonl"):
        return candidate
    return None


def _tail_loop_runtime(wire_path, max_bytes=1_048_576):
    """从 wire.jsonl 尾部取最后一条主循环 llm.request 的 (modelAlias, thinkingEffort)。

    只认主循环（kind=="loop"）：compaction 等辅助请求用 CLI 默认模型单独发起，
    不代表会话实际配置；thinkingEffort 兜底取 config.update 等会话级记录。
    任一项取不到则对应元素为 None，全程静默容错。
    """
    last_model = None
    last_effort = None
    fallback_effort = None
    try:
        for line in _tail_lines(wire_path, max_bytes=max_bytes):
            if "thinkingEffort" not in line and "modelAlias" not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "llm.request" and event.get("kind") == "loop":
                if event.get("modelAlias"):
                    last_model = str(event["modelAlias"])
                if event.get("thinkingEffort"):
                    last_effort = str(event["thinkingEffort"])
            elif event.get("thinkingEffort"):
                fallback_effort = str(event["thinkingEffort"])
    except Exception:
        pass
    return last_model, last_effort or fallback_effort


def _tail_thinking_effort(wire_path, max_bytes=1_048_576):
    """从 wire.jsonl 尾部取最后一次实际生效的 thinkingEffort（无则 None）。"""
    return _tail_loop_runtime(wire_path, max_bytes)[1]


def harvest_session_thinking(session_id):
    """收割指定 kimi 会话最近实际生效的思考档（接管时读取原会话用）。

    返回 wire 原样字符串（low/high/max/on…）；文件缺失、无记录、异常均返回 None。
    """
    wire_path = _kimi_wire_path(session_id)
    if not wire_path or not wire_path.exists():
        return None
    return _tail_thinking_effort(wire_path)


def harvest_kimi_context_usage(metadata):
    """从 ACP 会话 wire.jsonl 收割最后一轮 usage.record 与实际模型/思考档，更新 metadata。

    计算口径：inputOther + inputCacheRead + inputCacheCreation。
    实际值取尾部最后一条主循环 llm.request，写入 metadata["actual_model"] /
    metadata["actual_thinking"]。任何异常（文件缺失、JSON 损坏、无记录、
    模型未知）均静默返回，不阻塞终态收割与页面渲染。
    """
    session_id = metadata.get("kimi_session_id")
    if not session_id or metadata.get("agent") != "kimi" or metadata.get("kimi_engine") != "acp":
        return
    wire_path = _kimi_wire_path(session_id)
    if not wire_path or not wire_path.exists():
        return
    actual_model, effort = _tail_loop_runtime(wire_path)
    if effort:
        metadata["actual_thinking"] = effort
    if actual_model:
        metadata["actual_model"] = actual_model
    last_record = None
    try:
        for line in _tail_lines(wire_path, max_bytes=1_048_576):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "usage.record" and event.get("usageScope") == "turn":
                last_record = event
    except Exception:
        return
    if not last_record:
        return
    usage = last_record.get("usage") or {}
    try:
        used = sum(int(usage.get(key, 0)) for key in ("inputOther", "inputCacheRead", "inputCacheCreation"))
    except (TypeError, ValueError):
        return
    if used <= 0:
        return
    metadata["context_used"] = used
    model = last_record.get("model") or metadata.get("model") or ""
    limit = _KIMI_CONTEXT_LIMITS.get(model)
    if limit:
        metadata["context_limit"] = limit
    else:
        metadata.pop("context_limit", None)


def render_tool_groups(task_id, active=False):
    """把 output.jsonl 中连续 role=tool 行聚合为可展开卡片 HTML。

    - 通过同文件内前序 assistant 事件的 tool_calls 匹配工具名与参数。
    - 标题行显示"N 个工具调用 · 进行中/已完成"，默认收起。
    - 所有用户可见字段均经 html.escape 转义，与现有渲染路径一致。
    """
    path = _task_dir(task_id) / "output.jsonl"
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    tool_calls_map = {}
    groups = []
    current_group = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            if current_group:
                groups.append(current_group)
                current_group = []
            continue
        if not isinstance(event, dict):
            if current_group:
                groups.append(current_group)
                current_group = []
            continue
        if event.get("role") == "assistant":
            for tc in event.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                call_id = tc.get("id") or fn.get("id") or ""
                if not call_id:
                    continue
                arguments = fn.get("arguments") if "arguments" in fn else tc.get("arguments", "")
                if isinstance(arguments, (dict, list)):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                tool_calls_map[call_id] = {
                    "name": fn.get("name") or tc.get("name") or "工具调用",
                    "arguments": str(arguments).strip(),
                }
            if current_group:
                groups.append(current_group)
                current_group = []
        elif event.get("role") == "tool":
            current_group.append(event)
        else:
            if current_group:
                groups.append(current_group)
                current_group = []
    if current_group:
        groups.append(current_group)
    if not groups:
        return ""
    header_status = "进行中" if active else "已完成"
    parts = ["<div class='tool-groups'>"]
    for group in groups:
        count = len(group)
        summary_text = f"{count} 个工具调用 · {header_status}"
        parts.append("<details class='tool-group'>")
        parts.append("<summary>" + html_escape.escape(summary_text) + "</summary>")
        parts.append("<ul class='tool-list'>")
        for event in group:
            call_id = event.get("tool_call_id") or ""
            info = tool_calls_map.get(call_id, {})
            name = info.get("name") or "工具调用"
            arguments = info.get("arguments", "")
            if len(arguments) > 160:
                arguments = arguments[:160] + "…"
            content = str(event.get("content") or "").strip()
            lower = content.lower()
            if lower in {"failed", "failure", "error"}:
                status = "失败"
                content = ""
                status_class = "failed"
            elif lower in {"completed", "success", "ok"}:
                status = "完成"
                content = ""
                status_class = ""
            else:
                status = "完成"
                status_class = ""
            summary = content.replace("\n", " ")
            if len(summary) > 180:
                summary = summary[:180] + "…"
            parts.append("<li><div class='tool-row'>")
            parts.append("<div class='tool-name'>" + html_escape.escape(name) + "</div>")
            parts.append("<div class='tool-meta'>")
            parts.append("<span class='tool-status" + (" " + status_class if status_class else "") + "'>" + html_escape.escape(status) + "</span>")
            if arguments:
                parts.append("<span class='tool-args'>" + html_escape.escape(arguments) + "</span>")
            parts.append("</div>")
            if summary:
                parts.append("<div class='tool-summary'>" + html_escape.escape(summary) + "</div>")
            parts.append("</div></li>")
        parts.append("</ul></details>")
    parts.append("</div>")
    return "".join(parts)


def recent():
    if not ROOT.exists():
        return []
    items = []
    for path in ROOT.iterdir():
        task = path / "task.json"
        if task.exists():
            metadata = json.loads(task.read_text(encoding="utf-8"))
            if metadata.get("status") != "draft":
                items.append(metadata)
    return sorted(items, key=lambda item: item["created"], reverse=True)[:20]


def for_project(project_id):
    if not ROOT.exists():
        return []
    items = []
    for path in ROOT.iterdir():
        task = path / "task.json"
        if task.exists():
            metadata = json.loads(task.read_text(encoding="utf-8"))
            if metadata.get("project_id") == project_id:
                items.append(metadata)
    return sorted(items, key=lambda item: item["created"], reverse=True)


def thread(task_id):
    current, _, _ = get(task_id)
    chain = [current]
    seen = {current["id"]}
    while current.get("parent_id") and current["parent_id"] not in seen:
        current, _, _ = get(current["parent_id"])
        seen.add(current["id"])
        chain.append(current)
    return list(reversed(chain))


def thread_blocking(task_id):
    """对话链上是否还有阻塞轮（运行/排队/停止中/等授权/后台执行）。"""
    try:
        return any(t.get("status") in BLOCKING_STATUSES for t in thread(task_id))
    except Exception:
        return False


def kick_held_turns():
    """对外入口：扫描并启动可续发的挂起轮（幂等）。

    提交挂起轮后兜底调用一次，防"判定阻塞后活动轮恰好结束"的竞态。
    """
    _maybe_start_held()


def _maybe_start_held():
    """启动祖先链已不再阻塞的挂起轮（held）。

    排队自动续发：追问在对话有活动轮时先挂起，活动轮到终态后
    自动接续，避免 ACP 同会话并发 resume 互相踩踏
    （acp_conflict_fallback 回退 legacy 的触发窗口随之消失）。
    全局扫描幂等：候选按创建序逐条复查，启动前去锁内双检并摘除标记。
    """
    try:
        items = recent_all()
    except OSError:
        return
    by_id = {item.get("id"): item for item in items}
    for item in sorted(items, key=lambda entry: entry.get("created", "")):
        if not item.get("held") or item.get("status") != "queued":
            continue
        blocked = False
        current = item
        seen = {item.get("id")}
        while current.get("parent_id") and current["parent_id"] not in seen:
            seen.add(current["parent_id"])
            parent = by_id.get(current["parent_id"])
            if parent is None:
                break
            if parent.get("status") in BLOCKING_STATUSES:
                blocked = True
                break
            current = parent
        if blocked:
            continue
        with _METADATA_LOCK:
            try:
                latest = _read_metadata(item["id"])
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if not latest.get("held") or latest.get("status") != "queued":
                continue
            latest.pop("held", None)
            _write_metadata_locked(item["id"], latest)
        try:
            start(item["id"])
            _audit("held_turn_started", {"task_id": item["id"]})
        except Exception as error:
            _audit("held_turn_start_failed",
                   {"task_id": item["id"], "error": repr(error)[:200]})


def latest_in_thread(root_id, items=None):
    if items is None:
        items = recent_all()
    # Build a parent->children index once to avoid O(n^2) disk scans.
    by_parent = {}
    for item in items:
        by_parent.setdefault(item.get("parent_id"), []).append(item)
    current_id = root_id
    while True:
        children = by_parent.get(current_id, [])
        if not children:
            return current_id
        current_id = max(children, key=lambda item: item["created"])["id"]


def recent_all():
    if not ROOT.exists():
        return []
    items = []
    for path in ROOT.iterdir():
        task = path / "task.json"
        if task.exists():
            items.append(json.loads(task.read_text(encoding="utf-8")))
    return sorted(items, key=lambda item: item["created"], reverse=True)



# ── Persistent cache for conversation_roots ──
_HISTORY_CACHE = ROOT / "_history_index.json"
_CACHE_TTL = 300  # seconds; rebuild at most every 5 minutes
_roots_cache = {"mtime": 0, "data": None, "built_at": 0}


def conversation_roots():
    """Return root conversations from persistent cache, rebuilt when stale."""
    global _roots_cache
    try:
        dir_mtime = os.stat(ROOT).st_mtime
    except OSError:
        dir_mtime = 0
    now = time.time()

    # Try persistent cache first
    if _roots_cache["data"] is None and os.path.exists(_HISTORY_CACHE):
        try:
            with open(_HISTORY_CACHE, "r", encoding="utf-8") as fh:
                saved = json.loads(fh.read())
            if saved.get("mtime") == dir_mtime and (now - saved.get("built_at", 0)) < _CACHE_TTL:
                _roots_cache["data"] = saved["roots"]
                _roots_cache["mtime"] = dir_mtime
                _roots_cache["built_at"] = saved["built_at"]
                return _roots_cache["data"]
        except Exception:
            pass

    # Memory cache fallback
    if _roots_cache["data"] is not None and dir_mtime == _roots_cache["mtime"] and (now - _roots_cache["built_at"]) < _CACHE_TTL:
        return _roots_cache["data"]

    # Rebuild
    items = [item for item in recent_all() if item.get("status") != "draft"]
    by_id = {item["id"]: item for item in items}

    # Pre-load project names once to avoid repeated .codex-project.json reads.
    project_names = {}
    for item in items:
        pid = item.get("project_id")
        if pid and pid not in project_names:
            try:
                from . import projects
                project_names[pid] = projects.get(pid)["name"]
            except Exception:
                project_names[pid] = None

    roots = []
    for item in items:
        current = item
        seen = set()
        while current.get("parent_id") in by_id and current["parent_id"] not in seen:
            seen.add(current["id"])
            current = by_id[current["parent_id"]]
        if current["id"] == item["id"]:
            latest_id = latest_in_thread(item["id"], items=items)
            latest = by_id.get(latest_id, item)
            row = dict(item)
            row["latest_id"] = latest_id
            row["latest_status"] = latest.get("status", item.get("status"))
            row["latest_activity"] = latest.get("finished") or latest.get("started") or latest.get("created")
            pid = row.get("project_id")
            if pid and project_names.get(pid):
                row["project_name"] = project_names[pid]
            roots.append(row)
    roots.sort(key=lambda item: item.get("latest_activity", item["created"]), reverse=True)
    result = sorted(roots, key=lambda item: (not item.get("pinned", False), item.get("sort_order", 0)))

    _roots_cache["mtime"] = dir_mtime
    _roots_cache["data"] = result
    _roots_cache["built_at"] = now
    # Persist to disk for cross-request cache
    try:
        with open(_HISTORY_CACHE, "w", encoding="utf-8") as fh:
            json.dump({"mtime": dir_mtime, "built_at": now, "roots": result}, fh, ensure_ascii=False)
    except Exception:
        pass
    return result



def update_roots(root_ids, pinned=None):
    for index, root_id in enumerate(root_ids):
        path = _task_dir(root_id) / "task.json"
        metadata = json.loads(path.read_text(encoding="utf-8"))
        metadata["sort_order"] = index
        if pinned is not None:
            metadata["pinned"] = pinned
        path.write_text(json.dumps(metadata, ensure_ascii=True), encoding="utf-8")


def thread_members(root_id):
    chain_ids = {root_id}
    changed = True
    all_items = recent_all()
    while changed:
        changed = False
        for item in all_items:
            if item.get("parent_id") in chain_ids and item["id"] not in chain_ids:
                chain_ids.add(item["id"])
                changed = True
    return [item for item in all_items if item["id"] in chain_ids]


def delete_thread(root_id):
    members = thread_members(root_id)
    for metadata in members:
        if metadata.get("status") in BLOCKING_STATUSES:
            raise ValueError("运行中的对话不能删除。")
    for metadata in members:
        shutil.rmtree(_task_dir(metadata["id"]))
    attachment = INBOX_CONVERSATIONS / root_id
    if attachment.exists():
        shutil.rmtree(attachment)


def assign_thread(task_id, project_id):
    chain = thread(task_id)
    for metadata in chain:
        metadata["project_id"] = project_id
        (_task_dir(metadata["id"]) / "task.json").write_text(json.dumps(metadata, ensure_ascii=True), encoding="utf-8")
    return chain


def attachment_directory(task_id):
    chain = thread(task_id)
    root = chain[0]
    candidate = Path(root["workdir"]).resolve()
    if candidate.parent == INBOX_CONVERSATIONS.resolve() and candidate.is_dir():
        return candidate
    return None
