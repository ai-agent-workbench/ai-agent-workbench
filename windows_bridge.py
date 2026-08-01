import argparse
import base64
import json
import os
import re
import secrets
import signal
import subprocess
import time
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath


TASK_ROOT = Path(os.environ.get(
    "CODEX_WEB_TASK_ROOT",
    str(Path.home() / ".local" / "share" / "codex-web" / "tasks"),
))
WINDOWS_KEY = os.environ.get("CODEX_WEB_WINDOWS_KEY", str(Path.home() / ".ssh" / "windows_ed25519"))
WINDOWS_HOST = os.environ.get("CODEX_WEB_WINDOWS_HOST", "user@127.0.0.1")
WINDOWS_SSH_PORT = os.environ.get("CODEX_WEB_WINDOWS_SSH_PORT", "58231")
APPROVAL_FILE = "windows-approval.json"
AUDIT_FILE = "windows-audit.jsonl"
MAX_TEXT_BYTES = 1024 * 1024
SAFE_NAME = re.compile(r"^[A-Za-z0-9_. -]{1,128}$")
SAFE_COMMAND = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
SAFE_MODEL = re.compile(r"^[A-Za-z0-9._:/-]{1,200}$")
SAFE_TASK_ID = re.compile(r"^[a-f0-9]{24}$")
TRUST_FILE = "windows-trust.json"
TRUST_DURATION = timedelta(minutes=30)
TRUST_EXTRA = timedelta(seconds=30)


INSPECT_ACTIONS = {
    "system_summary": "读取 Windows 版本、主机名、内存和磁盘概况",
    "ollama_status": "读取 Windows Ollama 命令、进程和本机 API 状态",
    "ollama_models": "读取 Windows Ollama 已安装模型列表",
    "process_list": "读取 Windows 进程列表，可按名称筛选",
    "service_status": "读取一个 Windows 服务的状态",
    "path_info": "读取允许目录内文件或目录的属性",
    "command_exists": "检查一个 Windows 命令是否存在",
}

EXECUTE_ACTIONS = {
    "ollama_pull": "下载一个 Ollama 模型",
    "service_control": "启动、停止或重启一个 Windows 服务",
    "process_start": "启动允许目录内的 Windows 程序",
    "create_directory": "在允许目录内创建目录",
    "write_text": "在允许目录内写入 UTF-8 文本文件",
    "remove_path": "删除允许目录内的文件或目录",
}

LOCAL_EXECUTE_ACTIONS = {
    "restart_codex_web": "重启 codex-web 服务（uvicorn worker）",
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def encoded_powershell(script):
    # Suppress PowerShell progress/CLIXML noise that the bridge doesn't need
    script = "$ProgressPreference = 'SilentlyContinue'; $ErrorActionPreference = 'Continue'; " + script
    return base64.b64encode(script.encode("utf-16le")).decode("ascii")


def run_windows(script, timeout=45):
    command = [
        "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-i", WINDOWS_KEY, "-p", WINDOWS_SSH_PORT, WINDOWS_HOST,
        "powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
        encoded_powershell("$ErrorActionPreference='Stop';" + script),
    ]
    with tempfile.TemporaryFile() as output_file:
        process = subprocess.Popen(
            command, stdout=output_file, stderr=subprocess.STDOUT, start_new_session=True
        )
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise RuntimeError("Windows 操作超过允许执行时间。")
        output_file.seek(0, os.SEEK_END)
        size = output_file.tell()
        output_file.seek(max(0, size - 16000))
        output = output_file.read().decode("utf-8", errors="replace").strip()
        # Strip PowerShell CLIXML progress/noise lines
        import re as _re
        output = _re.sub(r'<#.*?#>', '', output, flags=_re.DOTALL)
        output = _re.sub(r'</?Objs[^>]*>', '', output)
        output = _re.sub(r'</?Objs[^>]*/>', '', output)
        output = "\n".join(line for line in output.split("\n") if not line.startswith("#<") and not line.startswith("<Objs"))
        output = output.strip()
    if returncode != 0:
        raise RuntimeError(output[-4000:] or "Windows 命令执行失败。")
    return output


def required_string(arguments, name, pattern=None, maximum=4096):
    value = arguments.get(name)
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(name + " 参数无效。")
    if pattern and not pattern.fullmatch(value):
        raise ValueError(name + " 参数包含不允许的字符。")
    return value


def optional_string(arguments, name, pattern=None, maximum=4096):
    value = arguments.get(name, "")
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(name + " 参数无效。")
    if value and pattern and not pattern.fullmatch(value):
        raise ValueError(name + " 参数包含不允许的字符。")
    return value


def allowed_windows_path(value, allow_root=False):
    raw = required_string({"path": value}, "path", maximum=1024).replace("/", "\\")
    if any(character in raw for character in "\x00\r\n"):
        raise ValueError("Windows 路径无效。")
    path = PureWindowsPath(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Windows 路径必须是绝对路径且不能包含 ..。")
    normalized = str(path)
    folded = normalized.casefold()
    # Windows 侧允许范围：D:\ 整盘 + 环境变量指定的桌面目录（CODEX_WEB_WINDOWS_DESKTOP）。
    desktop_root = os.environ.get("CODEX_WEB_WINDOWS_DESKTOP", r"C:\Users\user\Desktop")
    desktop_root = str(PureWindowsPath(desktop_root.replace("/", "\\"))).casefold()
    roots = ("d:", desktop_root)
    if not any(folded == root or folded.startswith(root + "\\") for root in roots):
        raise ValueError("Windows 路径只允许 D:\\ 或配置的桌面目录（CODEX_WEB_WINDOWS_DESKTOP）下的位置。")
    if not allow_root and folded.rstrip("\\") in roots:
        raise ValueError("不允许直接操作允许范围的根目录。")
    return normalized


def inspection_script(action, arguments):
    if action == "system_summary":
        return (
            "$os=Get-CimInstance Win32_OperatingSystem;"
            "$disks=Get-CimInstance Win32_LogicalDisk -Filter \"DriveType=3\"|"
            "Select-Object DeviceID,Size,FreeSpace;"
            "[ordered]@{ComputerName=$env:COMPUTERNAME;Caption=$os.Caption;"
            "Version=$os.Version;TotalMemory=$os.TotalVisibleMemorySize*1KB;"
            "FreeMemory=$os.FreePhysicalMemory*1KB;Disks=$disks}|ConvertTo-Json -Depth 4"
        )
    if action == "ollama_status":
        return (
            "$command=Get-Command ollama -ErrorAction SilentlyContinue;"
            "$process=Get-Process ollama -ErrorAction SilentlyContinue|"
            "Select-Object Id,ProcessName,StartTime;"
            "$api=$null;try{$api=Invoke-RestMethod http://127.0.0.1:11434/api/version -TimeoutSec 3}catch{};"
            "[ordered]@{Installed=[bool]$command;Path=$command.Source;Process=$process;Api=$api}|"
            "ConvertTo-Json -Depth 5"
        )
    if action == "ollama_models":
        return (
            "$result=Invoke-RestMethod http://127.0.0.1:11434/api/tags -TimeoutSec 10;"
            "$result|ConvertTo-Json -Depth 6"
        )
    if action == "process_list":
        name = optional_string(arguments, "name", SAFE_NAME, 128)
        filter_part = "|Where-Object {$_.ProcessName -like " + ps_quote("*" + name + "*") + "}" if name else ""
        return (
            "Get-Process " + filter_part + "|Sort-Object CPU -Descending|Select-Object -First 100 "
            "Id,ProcessName,CPU,WorkingSet64,StartTime|ConvertTo-Json -Depth 3"
        )
    if action == "service_status":
        name = required_string(arguments, "name", SAFE_NAME, 128)
        return (
            "Get-Service -Name " + ps_quote(name) + "|"
            "Select-Object Name,DisplayName,Status,StartType|ConvertTo-Json -Depth 3"
        )
    if action == "path_info":
        path = allowed_windows_path(arguments.get("path"), allow_root=True)
        return (
            "$item=Get-Item -LiteralPath " + ps_quote(path) + ";"
            "$size=if($item.PSIsContainer){$null}else{$item.Length};"
            "[ordered]@{FullName=$item.FullName;Name=$item.Name;IsDirectory=$item.PSIsContainer;"
            "Length=$size;LastWriteTime=$item.LastWriteTime;Attributes=[string]$item.Attributes}|"
            "ConvertTo-Json -Depth 3"
        )
    if action == "command_exists":
        name = required_string(arguments, "name", SAFE_COMMAND, 128)
        return (
            "$command=Get-Command " + ps_quote(name) + " -ErrorAction SilentlyContinue;"
            "[ordered]@{Found=[bool]$command;Name=$command.Name;Source=$command.Source;"
            "CommandType=[string]$command.CommandType}|ConvertTo-Json -Depth 3"
        )
    raise ValueError("不支持的只读动作。")


def execute_script(action, arguments):
    if action in LOCAL_EXECUTE_ACTIONS:
        if action == "restart_codex_web":
            return None, LOCAL_EXECUTE_ACTIONS["restart_codex_web"], 60
        raise ValueError("不支持的本地动作。")
    if action == "ollama_pull":
        model = required_string(arguments, "model", SAFE_MODEL, 200)
        return "& ollama pull " + ps_quote(model), "下载 Ollama 模型 " + model, 21600
    if action == "service_control":
        name = required_string(arguments, "name", SAFE_NAME, 128)
        operation = required_string(arguments, "operation", re.compile(r"^(start|stop|restart)$"), 16)
        commands = {"start": "Start-Service", "stop": "Stop-Service", "restart": "Restart-Service"}
        script = commands[operation] + " -Name " + ps_quote(name) + " -PassThru|Select-Object Name,Status|ConvertTo-Json"
        labels = {"start": "启动", "stop": "停止", "restart": "重启"}
        return script, labels[operation] + " Windows 服务 " + name, 120
    if action == "process_start":
        path = allowed_windows_path(arguments.get("path"))
        raw_args = arguments.get("arguments", [])
        if not isinstance(raw_args, list) or len(raw_args) > 32 or any(not isinstance(item, str) or len(item) > 1024 for item in raw_args):
            raise ValueError("arguments 参数无效。")
        argument_list = "@(" + ",".join(ps_quote(item) for item in raw_args) + ")"
        script = (
            "$process=Start-Process -FilePath " + ps_quote(path) + " -ArgumentList " + argument_list + " -PassThru;"
            "$process|Select-Object Id,ProcessName|ConvertTo-Json"
        )
        return script, "启动 Windows 程序 " + path, 120
    if action == "create_directory":
        path = allowed_windows_path(arguments.get("path"))
        script = "New-Item -ItemType Directory -Force -Path " + ps_quote(path) + "|Select-Object FullName|ConvertTo-Json"
        return script, "创建 Windows 目录 " + path, 120
    if action == "write_text":
        path = allowed_windows_path(arguments.get("path"))
        content = required_string(arguments, "content", maximum=MAX_TEXT_BYTES)
        if len(content.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError("content 超过 1 MB 上限。")
        overwrite = arguments.get("overwrite", False)
        if not isinstance(overwrite, bool):
            raise ValueError("overwrite 参数无效。")
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        guard = "" if overwrite else "if(Test-Path -LiteralPath $path){throw '目标文件已存在'};"
        script = (
            "$path=" + ps_quote(path) + ";" + guard +
            "$bytes=[Convert]::FromBase64String(" + ps_quote(encoded) + ");"
            "[IO.File]::WriteAllBytes($path,$bytes);Get-Item -LiteralPath $path|"
            "Select-Object FullName,Length,LastWriteTime|ConvertTo-Json"
        )
        return script, ("覆盖" if overwrite else "写入") + " Windows 文本文件 " + path, 120
    if action == "remove_path":
        path = allowed_windows_path(arguments.get("path"))
        recursive = arguments.get("recursive", False)
        if not isinstance(recursive, bool):
            raise ValueError("recursive 参数无效。")
        recurse_flag = " -Recurse" if recursive else ""
        script = (
            "$item=Get-Item -LiteralPath " + ps_quote(path) + ";"
            "Remove-Item -LiteralPath $item.FullName -Force" + recurse_flag + ";"
            "[ordered]@{Removed=$true;Path=$item.FullName}|ConvertTo-Json"
        )
        return script, "删除 Windows 路径 " + path, 300
    raise ValueError("不支持的写入动作。")


def trust_path(task_id):
    if task_id == "interactive-cli":
        return TASK_ROOT / task_id / TRUST_FILE
    if not SAFE_TASK_ID.fullmatch(task_id):
        raise ValueError("任务 ID 无效。")
    directory = TASK_ROOT / task_id
    return directory / TRUST_FILE


def read_trust(task_id):
    try:
        path = trust_path(task_id)
    except ValueError:
        return None
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        trusted_until = datetime.fromisoformat(data["trusted_until"])
        if datetime.now(timezone.utc) >= trusted_until:
            path.unlink(missing_ok=True)
            return None
        return data
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return None


def write_trust(task_id):
    try:
        path = trust_path(task_id)
    except ValueError:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"trusted_until": (datetime.now(timezone.utc) + TRUST_DURATION + TRUST_EXTRA).isoformat()}
    temporary = path.with_suffix(".tmp-" + secrets.token_hex(4))
    temporary.write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)



def approval_path(task_id):
    if not SAFE_TASK_ID.fullmatch(task_id):
        raise ValueError("任务 ID 无效。")
    directory = TASK_ROOT / task_id
    if not (directory / "task.json").is_file():
        raise ValueError("任务不存在。")
    return directory / APPROVAL_FILE


def read_approval(task_id):
    path = approval_path(task_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def validated_approval(task_id):
    request = read_approval(task_id)
    if not request:
        return None
    if request.get("task_id") != task_id or not isinstance(request.get("id"), str):
        raise ValueError("Windows 请求绑定无效。")
    _, summary, timeout = execute_script(request.get("action"), request.get("arguments"))
    request["summary"] = summary
    request["timeout_seconds"] = timeout
    return request


def write_approval(task_id, request):
    path = approval_path(task_id)
    temporary = path.with_suffix(".tmp-" + secrets.token_hex(4))
    temporary.write_text(json.dumps(request, ensure_ascii=True), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def append_audit(task_id, event, detail):
    path = approval_path(task_id).with_name(AUDIT_FILE)
    record = {"time": now_iso(), "event": event, "detail": detail}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=True) + "\n")
    os.chmod(path, 0o600)


def create_approval(task_id, action, arguments, reason, source="windows"):
    script, summary, timeout = execute_script(action, arguments)
    existing = validated_approval(task_id)
    request_shape = {"action": action, "arguments": arguments, "source": source}
    if existing:
        if existing.get("status") == "pending":
            if {key: existing.get(key) for key in request_shape} == request_shape:
                return existing
            raise ValueError("当前轮次已有一个请求，请先处理该请求。")
        # Non-pending (executed/rejected/failed): clean up old request so a
        # new one can proceed. The trust window obsoleted the original check.
        path = approval_path(task_id)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    request = {
        "id": "wr_" + secrets.token_hex(12),
        "task_id": task_id,
        "status": "pending",
        "source": source,
        "action": action,
        "arguments": arguments,
        "reason": reason[:1000],
        "summary": summary,
        "created": now_iso(),
        "timeout_seconds": timeout,
    }
    write_approval(task_id, request)
    append_audit(task_id, "request_created", {
        "request_id": request["id"], "source": source, "action": action, "arguments": arguments, "reason": request["reason"],
    })
    # 注意：WS 审批广播不在此处做——此时任务状态尚未翻转 waiting_approval
    # （翻转在收割路径 _finish_from_process），前端收到即 reload 会拿到无卡片的
    # running 页（竞态，2026-07-28 验收实证）。广播挂点在 tasks._emit_terminal。
    return request


def _restart_codex_web_daemon(task_id, request_id):
    """Kill our own uvicorn worker after a short delay.

    PIN approval has already been persisted and the HTTP response sent.
    SIGKILL produces a non-zero exit so systemd Restart=on-failure
    brings the service back. This thread dies with the process.
    """
    import time

    time.sleep(1.5)
    try:
        append_audit(task_id, "service_restart_initiated", {"request_id": request_id})
    except Exception:
        pass
    os.kill(os.getpid(), signal.SIGKILL)


def _execute_local_approval(task_id, request):
    request_id = request["id"]
    request["status"] = "executing"
    request["approved"] = now_iso()
    write_approval(task_id, request)
    append_audit(task_id, "execution_started", {
        "request_id": request_id, "action": request["action"], "source": "local",
    })
    if request["action"] == "restart_codex_web":
        threading.Thread(
            target=_restart_codex_web_daemon, args=(task_id, request_id), daemon=True
        ).start()
    return request


def execute_approval(task_id, request_id):
    request = validated_approval(task_id)
    if not request or request.get("id") != request_id or request.get("status") != "pending":
        raise ValueError("请求不存在或已经处理。")
    if request.get("source") == "local" or request["action"] in LOCAL_EXECUTE_ACTIONS:
        return _execute_local_approval(task_id, request)
    write_trust(task_id)
    script, summary, timeout = execute_script(request["action"], request["arguments"])
    request["status"] = "executing"
    request["approved"] = now_iso()
    write_approval(task_id, request)
    append_audit(task_id, "execution_started", {"request_id": request_id, "action": request["action"]})
    try:
        result = run_windows(script, timeout=timeout)
        request.update({"status": "executed", "executed": now_iso(), "result": result, "summary": summary})
    except Exception as error:
        request.update({"status": "failed", "executed": now_iso(), "result": str(error)[-4000:]})
    write_approval(task_id, request)
    append_audit(task_id, "execution_finished", {
        "request_id": request_id, "status": request["status"], "result": request.get("result", "")[-2000:],
    })
    return request


def reject_approval(task_id, request_id):
    request = validated_approval(task_id)
    if not request or request.get("id") != request_id or request.get("status") != "pending":
        raise ValueError("请求不存在或已经处理。")
    request.update({"status": "rejected", "rejected": now_iso()})
    write_approval(task_id, request)
    append_audit(task_id, "request_rejected", {"request_id": request_id, "action": request["action"]})
    return request


def tool_result(value, is_error=False):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    result = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


class McpServer:
    def __init__(self, task_id):
        self.task_id = task_id

    def tools(self):
        return [
            {
                "name": "windows_inspect",
                "description": (
                    "通过工作台现有 SSH 通道读取 Windows 状态。仅当当前 WSL 任务需要 Windows、PowerShell、"
                    "Ollama、Windows 服务/进程或 Windows 文件属性时调用；不要尝试从 WSL 直接启动 powershell.exe。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": list(INSPECT_ACTIONS)},
                        "name": {"type": "string", "description": "进程筛选、服务名或命令名"},
                        "path": {"type": "string", "description": "D:\\ 或配置的桌面目录下的绝对路径"},
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
                "annotations": {"title": "读取 Windows 状态", "readOnlyHint": True, "openWorldHint": False, "destructiveHint": False},
            },
            {
                "name": "windows_execute",
                "description": (
                    "申请一个结构化 Windows 写操作。此工具只创建持久审批请求，不会立即改动 Windows；"
                    "调用后告诉用户请求已在手机工作台等待 PIN，不要改用 shell 或 powershell.exe 绕过审批。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": list(EXECUTE_ACTIONS)},
                        "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
                        "name": {"type": "string"},
                        "operation": {"type": "string", "enum": ["start", "stop", "restart"]},
                        "model": {"type": "string"},
                        "path": {"type": "string"},
                        "arguments": {"type": "array", "items": {"type": "string"}, "maxItems": 32},
                        "content": {"type": "string", "maxLength": MAX_TEXT_BYTES},
                        "overwrite": {"type": "boolean"},
                        "recursive": {"type": "boolean"},
                    },
                    "required": ["action", "reason"],
                    "additionalProperties": False,
                },
                "annotations": {"title": "申请 Windows 操作", "readOnlyHint": False, "openWorldHint": False, "destructiveHint": False},
            },
            {
                "name": "local_execute",
                "description": (
                    "申请一个结构化本地（WSL）写操作。此工具只创建持久审批请求，不会立即执行；"
                    "调用后告诉用户请求已在手机工作台等待 PIN，不要改用 shell 命令绕过审批。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": list(LOCAL_EXECUTE_ACTIONS)},
                        "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
                    },
                    "required": ["action", "reason"],
                    "additionalProperties": False,
                },
                "annotations": {"title": "申请本地操作", "readOnlyHint": False, "openWorldHint": False, "destructiveHint": False},
            },
        ]

    def call_tool(self, name, arguments):
        try:
            if name == "windows_inspect":
                action = required_string(arguments, "action")
                if action not in INSPECT_ACTIONS:
                    raise ValueError("不支持的只读动作。")
                append_audit(self.task_id, "inspection_started", {"action": action, "arguments": arguments})
                try:
                    for _attempt in range(2):
                        try:
                            output = run_windows(inspection_script(action, arguments), timeout=30)
                            break
                        except Exception:
                            if _attempt == 1:
                                raise
                except Exception as error:
                    append_audit(self.task_id, "inspection_finished", {"action": action, "status": "failed", "error": str(error)[-2000:]})
                    raise
                append_audit(self.task_id, "inspection_finished", {"action": action, "status": "succeeded", "result": output[-2000:]})
                return tool_result({"status": "ok", "action": action, "result": output})
            if name == "windows_execute":
                action = required_string(arguments, "action")
                reason = required_string(arguments, "reason", maximum=1000)
                if action not in EXECUTE_ACTIONS:
                    raise ValueError("不支持的写入动作。")
                clean_arguments = {key: value for key, value in arguments.items() if key not in {"action", "reason"}}
                # Session-level trust: if a prior approval was executed via
                # the web app within the trust window, skip approval and
                # execute directly.
                if read_trust(self.task_id):
                    script, summary, timeout = execute_script(action, clean_arguments)
                    append_audit(self.task_id, "trusted_execution_started", {
                        "action": action, "arguments": clean_arguments,
                    })
                    try:
                        result = run_windows(script, timeout=timeout)
                        append_audit(self.task_id, "trusted_execution_finished", {
                            "action": action, "status": "succeeded",
                            "result": result[-2000:],
                        })
                        return tool_result({"status": "ok", "action": action, "summary": summary, "result": result})
                    except Exception as error:
                        append_audit(self.task_id, "trusted_execution_finished", {
                            "action": action, "status": "failed",
                            "error": str(error)[-2000:],
                        })
                        return tool_result({"status": "error", "action": action, "message": str(error)}, is_error=True)
                else:
                    request = create_approval(self.task_id, action, clean_arguments, reason)
                    return tool_result({
                        "status": "approval_required", "request_id": request["id"],
                        "summary": request["summary"], "message": "请求已持久保存，等待用户在手机工作台输入 PIN。",
                    })
            if name == "local_execute":
                action = required_string(arguments, "action")
                reason = required_string(arguments, "reason", maximum=1000)
                if action not in LOCAL_EXECUTE_ACTIONS:
                    raise ValueError("不支持的本地动作。")
                request = create_approval(self.task_id, action, {}, reason, source="local")
                return tool_result({
                    "status": "approval_required", "request_id": request["id"],
                    "summary": request["summary"], "message": "请求已持久保存，等待用户在手机工作台输入 PIN。",
                })
            raise ValueError("未知工具。")
        except Exception as error:
            return tool_result({"status": "error", "message": str(error)}, is_error=True)

    def handle(self, message):
        method = message.get("method")
        if method == "initialize":
            requested = message.get("params", {}).get("protocolVersion", "2025-06-18")
            return {"protocolVersion": requested, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "codex-web-windows-bridge", "version": "0.1.0"}}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self.tools()}
        if method == "tools/call":
            params = message.get("params", {})
            return self.call_tool(params.get("name"), params.get("arguments") or {})
        if method and method.startswith("notifications/"):
            return None
        raise ValueError("不支持的 MCP 方法。")

    def run(self):
        for line in sys.stdin:
            try:
                message = json.loads(line)
                result = self.handle(message)
                if "id" not in message or result is None:
                    continue
                response = {"jsonrpc": "2.0", "id": message["id"], "result": result}
            except Exception as error:
                response = {"jsonrpc": "2.0", "id": message.get("id") if "message" in locals() else None, "error": {"code": -32603, "message": str(error)}}
            sys.stdout.write(json.dumps(response, ensure_ascii=True) + "\n")
            sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=False, default=None)
    parser.add_argument("--stdio", action="store_true", help="Run in generic STDIO MCP mode for interactive CLI")
    arguments = parser.parse_args()
    if arguments.stdio or arguments.task_id is None:
        # Interactive CLI mode: use a fixed generic task directory.
        # windows_execute / local_execute will still create approval requests
        # but they cannot be approved without the web UI.
        task_dir = Path(TASK_ROOT) / "interactive-cli"
        task_dir.mkdir(parents=True, exist_ok=True)
        McpServer("interactive-cli").run()
    else:
        approval_path(arguments.task_id)
        McpServer(arguments.task_id).run()


if __name__ == "__main__":
    main()
