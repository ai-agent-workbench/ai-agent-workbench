"""kimi ACP turn runner —— `kimi -p` 一次性进程的对讲机替代。

一轮一个进程：spawn `kimi acp` → initialize 握手 → session/new 或
session/resume → session/prompt → 把 ACP 事件流合成现行 stream-json 行写入
任务 output.jsonl → 轮结束杀进程。进程生命周期（pid/reaper/看门狗）仍归
tasks.py，本模块只管轮内协议。

设计约束：
- 纯线程模型（服务是同步 FastAPI + 线程，不引 asyncio）。
- 不 import tasks（避免循环依赖），审计经 audit_fn 回调。
- 实测依据：02-使用优化/2026-07-27-kimi-acp探针-staging/实测协议笔记.md
  （cancel 0.01s 停流；resume 无回放；#1865 空转需防；协议不符/401 fail-fast）。
"""
import json
import os
import select
import signal
import subprocess
import threading
import time

PROTOCOL_VERSION = 1
_KILL_GRACE_SECONDS = 3.0
_THINK_FLUSH_INTERVAL = 2.0
_TOOL_RESULT_MAX_CHARS = 4000

_RUNNERS = {}
_RUNNERS_LOCK = threading.Lock()


class AcpError(RuntimeError):
    """协议级失败（握手/认证/协议漂移）。消息对用户可读。"""


def request_cancel(task_id):
    """向在跑的 ACP 轮写 session/cancel。进程不在或未就绪返回 False。"""
    with _RUNNERS_LOCK:
        runner = _RUNNERS.get(task_id)
    return runner.cancel() if runner else False


def start_turn(task_id, command, env, cwd, output_path, prompt,
               session_id=None, model=None, default_model=None,
               thinking=None, plan_mode=False, audit_fn=None,
               event_fn=None):
    """spawn `kimi acp` 并完成握手+建会话，然后后台跑一轮 prompt。

    同步段（initialize/session 建立，约 1-2s）内抛 AcpError —— 调用方据此
    诚实判任务失败（401/协议不符红线）。返回 (Popen, session_id)。
    event_fn(task_id, obj)：每个 assistant 段落行落盘时回调（WS 内容事件挂点）。
    """
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, cwd=cwd, start_new_session=True,
    )
    runner = _Runner(task_id, proc, cwd, output_path, prompt, session_id,
                     model, default_model, thinking, plan_mode,
                     audit_fn, event_fn)
    try:
        runner.begin()
    except Exception:
        runner.kill_process()
        raise
    with _RUNNERS_LOCK:
        _RUNNERS[task_id] = runner
    threading.Thread(target=runner.run_prompt, daemon=True,
                     name="acp-turn-" + task_id[:8]).start()
    threading.Thread(target=runner.pump_stderr, daemon=True,
                     name="acp-stderr-" + task_id[:8]).start()
    return proc, runner.session_id


class _Runner:
    def __init__(self, task_id, proc, cwd, output_path, prompt, session_id,
                 model, default_model, thinking=None, plan_mode=False,
                 audit_fn=None, event_fn=None):
        self.task_id = task_id
        self.proc = proc
        self.cwd = cwd
        self.prompt = prompt
        self.resume_session_id = session_id
        self.model = model
        self.default_model = default_model
        self.thinking = thinking
        self.plan_mode = plan_mode
        self.audit = audit_fn or (lambda event, detail=None: None)
        self.event_fn = event_fn
        self.session_id = None
        self._next_id = 0
        self._pending = {}            # rid -> {"event": Event, "result": ..., "error": ...}
        self._write_lock = threading.Lock()
        self._out_lock = threading.Lock()
        self._out = open(output_path, "ab")
        self._text_buf = []
        self._think_buf = []
        self._last_think_flush = time.monotonic()
        self._saw_text = False
        self._saw_tool = False
        self._tool_titles = {}
        self._prompt_in_flight = False
        self._stop_reason = None
        self._stdout_eof = False

    # ── 同步段：握手 + 建会话 ─────────────────────────────────────────────

    def begin(self):
        init = self._call("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "clientInfo": {"name": "codex-web-workbench", "title": "Codex Web 工作台",
                           "version": "1.0"},
            "clientCapabilities": {
                # 文件读写交给 kimi 在 bwrap 沙盒内本地执行，只走审批；
                # 适配层不做路径校验，攻击面更小。
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
            },
        }, timeout=20)
        if not isinstance(init.get("agentCapabilities"), dict):
            self.audit("acp_protocol_mismatch", {"task_id": self.task_id,
                                                 "init": str(init)[:400]})
            raise AcpError("ACP 协议不符：initialize 响应缺 agentCapabilities")
        params = {"cwd": self.cwd, "mcpServers": []}
        if self.resume_session_id:
            resp = self._call("session/resume", {
                "sessionId": self.resume_session_id, **params}, timeout=30)
            self.session_id = self.resume_session_id
        else:
            resp = self._call("session/new", params, timeout=30)
            self.session_id = resp.get("sessionId")
            if not self.session_id:
                raise AcpError("session/new 响应无 sessionId")
        if self.model and self.default_model and self.model != self.default_model:
            try:
                self._call("session/set_config_option", {
                    "sessionId": self.session_id,
                    "configId": "model", "value": self.model}, timeout=15)
            except AcpError as error:
                # 切模型失败不致命：按默认模型继续，记审计保持诚实。
                self.audit("acp_model_override_failed", {
                    "task_id": self.task_id, "model": self.model, "error": str(error)[:200]})
        # thinking 显式指定才切换（空=跟随 CLI 默认）；失败不致命，按默认继续。
        # 注意 0.29.2 实测：configOptions 宣称默认 high，但 ACP 会话初始实为 max——
        # 不得以 != "high" 为锚，否则用户选 High 不发切换、货不对板（staging wire 实证）。
        if self.thinking:
            try:
                self._call("session/set_config_option", {
                    "sessionId": self.session_id,
                    "configId": "thinking", "value": self.thinking}, timeout=15)
            except AcpError as error:
                self.audit("acp_thinking_override_failed", {
                    "task_id": self.task_id, "thinking": self.thinking,
                    "error": str(error)[:200]})
        # plan 模式：只读规划，不执行工具。
        if self.plan_mode:
            try:
                self._call("session/set_config_option", {
                    "sessionId": self.session_id,
                    "configId": "mode", "value": "plan"}, timeout=15)
            except AcpError as error:
                self.audit("acp_mode_override_failed", {
                    "task_id": self.task_id, "error": str(error)[:200]})

    # ── 后台段：一轮 prompt ───────────────────────────────────────────────

    def run_prompt(self):
        try:
            self._prompt_in_flight = True
            result = self._call("session/prompt", {
                "sessionId": self.session_id,
                "prompt": [{"type": "text", "text": self.prompt}],
            }, timeout=None)
            self._stop_reason = result.get("stopReason")
            self._flush_text(final=True)
            self._flush_think(final=True)
            # #1865：失败回合会被静默按 end_turn 返回。全轮零正文零工具
            # 视为空转失败——不写 assistant 行，任务因无 final.txt 诚实判败。
            # cancelled 除外：思考期被急停的轮合法地零正文，不是失败。
            if self._stop_reason != "cancelled" and not self._saw_text and not self._saw_tool:
                self._write_line({"role": "meta", "type": "acp.empty_turn",
                                  "stopReason": self._stop_reason})
                self.audit("acp_empty_turn", {"task_id": self.task_id,
                                              "stopReason": self._stop_reason})
        except AcpError as error:
            self.audit("acp_turn_error", {"task_id": self.task_id,
                                          "error": str(error)[:400]})
            self._write_line({"role": "meta", "type": "acp.error",
                              "error": str(error)[:400]})
        except Exception as error:  # 适配层自身出错也不能留僵尸
            self.audit("acp_runner_error", {"task_id": self.task_id,
                                            "error": repr(error)[:400]})
            self._write_line({"role": "meta", "type": "acp.error",
                              "error": repr(error)[:400]})
        finally:
            self._prompt_in_flight = False
            self._write_line({
                "role": "meta", "type": "session.resume_hint",
                "session_id": self.session_id,
                "command": "kimi -r " + str(self.session_id),
                "content": "To resume this session: kimi -r " + str(self.session_id),
            })
            self.kill_process()
            with _RUNNERS_LOCK:
                _RUNNERS.pop(self.task_id, None)
            try:
                self._out.close()
            except OSError:
                pass

    def cancel(self):
        if not self._prompt_in_flight or not self.session_id:
            return False
        try:
            self._notify("session/cancel", {"sessionId": self.session_id})
            return True
        except Exception:
            return False

    def pump_stderr(self):
        """stderr 只做日志，原样追加 output.jsonl（与旧 spawn 的 stderr=STDOUT 一致）。"""
        try:
            while True:
                line = self.proc.stderr.readline()
                if not line:
                    break
                with self._out_lock:
                    self._out.write(line if line.endswith(b"\n") else line + b"\n")
                    self._out.flush()
        except (OSError, ValueError):
            pass

    def kill_process(self):
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + _KILL_GRACE_SECONDS
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return
            time.sleep(0.1)
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    # ── JSON-RPC 收发 ─────────────────────────────────────────────────────

    def _call(self, method, params, timeout):
        self._next_id += 1
        rid = self._next_id
        entry = {"event": threading.Event(), "result": None, "error": None}
        self._pending[rid] = entry
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = None if timeout is None else time.monotonic() + timeout
        while not entry["event"].is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                self._pending.pop(rid, None)
                raise AcpError(method + " 超时（%ss 无响应）" % timeout)
            if self.proc.poll() is not None and (self._stdout_eof or not self._readable(0)):
                self._pending.pop(rid, None)
                raise AcpError("kimi acp 进程意外退出（rc=%s）" % self.proc.returncode)
            self._pump_once(remaining)
        self._pending.pop(rid, None)
        if entry["error"] is not None:
            err = entry["error"]
            if isinstance(err, dict) and err.get("code") == -32000:
                raise AcpError("kimi 登录失效（auth_required），请在终端检查 kimi 登录状态")
            raise AcpError(method + " 被拒绝: " + json.dumps(err, ensure_ascii=False)[:300])
        return entry["result"]

    def _notify(self, method, params):
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _write(self, msg):
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def _readable(self, timeout):
        try:
            ready, _, _ = select.select([self.proc.stdout], [], [], timeout)
            return bool(ready)
        except (OSError, ValueError):
            return False

    def _pump_once(self, timeout):
        """读一行并分发。用 os.read 手动拼行，避免 BufferedReader 预读与 select 打架。"""
        if not hasattr(self, "_rx"):
            self._rx = b""
        while b"\n" not in self._rx:
            if self._stdout_eof:
                return
            if not self._readable(min(timeout, 1.0) if timeout is not None else 1.0):
                return
            try:
                chunk = os.read(self.proc.stdout.fileno(), 65536)
            except (OSError, ValueError):
                chunk = b""
            if not chunk:
                # EOF：进程死亡后管道恒「可读」，必须显式记 EOF，
                # 否则 _call 的死亡检测（poll() 非 None 且不可读）永不成立。
                self._stdout_eof = True
                return
            self._rx += chunk
        line, self._rx = self._rx.split(b"\n", 1)
        line = line.strip()
        if not line:
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return
        self._dispatch(msg)

    def _dispatch(self, msg):
        mid = msg.get("id")
        if mid is not None and ("result" in msg or "error" in msg):
            entry = self._pending.get(mid)
            if entry:
                entry["result"] = msg.get("result")
                entry["error"] = msg.get("error")
                entry["event"].set()
            return
        method = msg.get("method", "")
        params = msg.get("params") or {}
        if mid is not None and method:
            self._handle_server_request(mid, method, params)
        elif method == "session/update":
            self._on_update(params)

    def _handle_server_request(self, mid, method, params):
        if method == "session/request_permission":
            options = params.get("options") or []
            pick = next((o for o in options if o.get("kind") == "allow_once"), None) \
                or next((o for o in options if str(o.get("kind", "")).startswith("allow")), None) \
                or (options[0] if options else None)
            tool = (params.get("toolCall") or {}).get("title")
            self.audit("acp_permission_auto", {
                "task_id": self.task_id, "tool": tool,
                "granted": (pick or {}).get("optionId")})
            if pick:
                self._write({"jsonrpc": "2.0", "id": mid, "result": {
                    "outcome": {"outcome": "selected", "optionId": pick["optionId"]}}})
            else:
                self._write({"jsonrpc": "2.0", "id": mid, "result": {
                    "outcome": {"outcome": "cancelled"}}})
        else:
            self._write({"jsonrpc": "2.0", "id": mid,
                         "error": {"code": -32601, "message": "not implemented: " + method}})

    # ── 事件合成（保持现行 stream-json 形状，下游解析零改动） ─────────────

    def _on_update(self, params):
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        if kind == "plan":
            # plan 模式下的计划 update，原样落盘为 meta 行；结构未探明前保留完整 dict。
            self._write_line({"role": "meta", "type": "plan", "plan": update})
        elif kind == "agent_message_chunk":
            content = update.get("content") or {}
            if content.get("type") == "text":
                text = content.get("text", "")
                if text:
                    self._saw_text = True
                    self._text_buf.append(text)
        elif kind == "agent_thought_chunk":
            content = update.get("content") or {}
            text = content.get("text", "") if isinstance(content, dict) else ""
            if text:
                self._think_buf.append(text)
                if time.monotonic() - self._last_think_flush >= _THINK_FLUSH_INTERVAL:
                    self._flush_think()
        elif kind == "tool_call":
            self._saw_tool = True
            call_id = update.get("toolCallId") or ""
            title = update.get("title") or ""
            self._tool_titles[call_id] = title
            raw_input = update.get("rawInput")
            arguments = raw_input if isinstance(raw_input, str) else json.dumps(
                raw_input or {}, ensure_ascii=False)
            self._flush_text(tool_call={
                "type": "function", "id": call_id,
                "function": {"name": title, "arguments": arguments},
            })
        elif kind == "tool_call_update":
            status = update.get("status")
            if status in {"completed", "failed"}:
                call_id = update.get("toolCallId") or ""
                content = update.get("rawOutput")
                if content is None:
                    parts = update.get("content") or []
                    texts = []
                    for part in parts:
                        if isinstance(part, dict) and part.get("type") == "content":
                            inner = part.get("content") or {}
                            if inner.get("type") == "text":
                                texts.append(inner.get("text", ""))
                    content = "\n".join(t for t in texts if t) or status
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False)
                self._write_line({
                    "role": "tool", "tool_call_id": call_id,
                    "content": content[:_TOOL_RESULT_MAX_CHARS]})

    def _flush_text(self, tool_call=None, final=False):
        text = "".join(self._text_buf).strip()
        self._text_buf = []
        if not text and tool_call is None:
            return
        event = {"role": "assistant", "content": text}
        if tool_call is not None:
            event["tool_calls"] = [tool_call]
        self._write_line(event)

    def _flush_think(self, final=False):
        self._last_think_flush = time.monotonic()
        think = "".join(self._think_buf).strip()
        self._think_buf = []
        if not think:
            return
        self._write_line({"role": "assistant",
                          "content": [{"type": "think", "think": think[-800:]}]})

    def _write_line(self, obj):
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self._out_lock:
            self._out.write(data)
            self._out.flush()
        # 段落级内容事件挂点（WS 准实时推送）；回调异常不得影响输出落盘。
        if self.event_fn and obj.get("role") == "assistant":
            try:
                self.event_fn(self.task_id, obj)
            except Exception:
                pass
