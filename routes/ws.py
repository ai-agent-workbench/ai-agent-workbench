"""WebSocket real-time channel for task status, output, and approval events.

Design (per implementation plan):
- Single FastAPI WebSocket endpoint at /ws
- Connection manager tracks active clients
- Event types: task.status, task.output, approval.request, server.request
- Frontend JS uses WebSocket first, falls back to HTTP polling
"""

import asyncio
import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from shared.config import ENABLED_FILE
from shared.security import audit, logged_in

router = APIRouter()


class ConnectionManager:
    """Track active WebSocket clients and broadcast events."""

    def __init__(self):
        self._connections: list[WebSocket] = []
        # uvicorn 主循环，首次 connect 时捕获；收割/ACP 等工作线程经
        # push_threadsafe 把广播协程调度进来（线程内 get_event_loop 必然抛错，
        # 旧 _ws_broadcast_status 因此从未真正发出过事件）。
        self.loop = None

    async def connect(self, ws: WebSocket) -> None:
        self.loop = asyncio.get_running_loop()
        await ws.accept()
        self._connections.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self._connections:
            self._connections.remove(ws)

    async def broadcast(self, event_type: str, data: Any) -> None:
        payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
        dead: list[WebSocket] = []
        for ws in self._connections:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def broadcast_task_status(self, task_id: str, status: str, **extra) -> None:
        await self.broadcast("task.status", {"task_id": task_id, "status": status, **extra})

    async def broadcast_task_output(self, task_id: str, output_tail: str = None,
                                    summary: str = None, staged: str = None) -> None:
        # 只携带调用方给的字段，前端按字段存在性更新对应区域（undefined 不碰）。
        data = {"task_id": task_id}
        if output_tail is not None:
            data["output_tail"] = output_tail
        if summary is not None:
            data["summary"] = summary
        if staged is not None:
            data["staged"] = staged
        await self.broadcast("task.output", data)

    async def broadcast_approval(self, task_id: str, request: dict) -> None:
        await self.broadcast("approval.request", {"task_id": task_id, "request": request})

    async def broadcast_server_request(self, detail: dict) -> None:
        await self.broadcast("server.request", detail)


manager = ConnectionManager()


def push_threadsafe(coro) -> None:
    """从任意线程把广播协程调度进主循环；尚无 WS 连接（无循环）时静默丢弃。"""
    loop = manager.loop
    try:
        if loop is None or not loop.is_running():
            coro.close()
            return
        asyncio.run_coroutine_threadsafe(coro, loop)
    except RuntimeError:
        coro.close()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # 与 require_auth 同语义：TOTP 未启用或 codex_web_session cookie 无效即拒。
    # 红线：本鉴权未上线前，nginx 不得把 /ws 放行到任何非回环路径。
    if not ENABLED_FILE.exists() or not logged_in(ws):
        audit(ws, "ws_auth_failed")
        await ws.close(code=4401)
        return
    await manager.connect(ws)
    try:
        while True:
            # Keep connection alive; client may send pings
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text('{"type":"pong"}')
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(ws)
