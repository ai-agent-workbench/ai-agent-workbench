"""Application-layer rate limiting (in-memory — resets on restart).

Per the implementation plan:
- TOTP attempts:  5 per minute (global)
- API polling:   30 per minute per IP
- File operations: 20 per minute per IP
"""

import time
from collections import defaultdict
from typing import Callable

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


_WINDOW = 60  # seconds


class RateLimiter:
    """Sliding-window counter per key.  Not thread-safe — single async thread."""

    def __init__(self, max_requests: int):
        self._max = max_requests
        self._buckets: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str) -> bool:
        now = time.monotonic()
        bucket = self._buckets[key]
        # Evict stale timestamps
        cutoff = now - _WINDOW
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)
        if len(bucket) >= self._max:
            return False
        bucket.append(now)
        return True


class RateLimitMiddleware(BaseHTTPMiddleware):
    # Recognised buckets
    _totp = RateLimiter(5)       # /setup POST, /login POST
    _api = RateLimiter(30)       # /task/{id}/status
    _files = RateLimiter(20)     # /upload, /edit/write, /delete/execute, /move/execute

    async def dispatch(self, request: Request, call_next: Callable):
        path = request.url.path
        client_ip = request.client.host if request.client else "127.0.0.1"
        limiter: RateLimiter | None = None
        key: str = ""

        if path in ("/setup", "/login") and request.method == "POST":
            limiter = self._totp
            key = "totp"
        elif path.endswith("/status"):
            limiter = self._api
            key = client_ip
        elif any(
            path.endswith(suffix)
            for suffix in ("/upload", "/edit/write", "/delete/execute", "/move/execute")
        ):
            limiter = self._files
            key = client_ip

        if limiter is not None and not limiter.check(key):
            return JSONResponse(
                {"error": "请求过于频繁，请稍后再试。"},
                status_code=429,
            )

        return await call_next(request)
