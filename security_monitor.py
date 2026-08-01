import json
import os
import re
import subprocess
import time
from collections import defaultdict, deque
from pathlib import Path

LOG_PATH = Path(os.environ.get(
    "CODEX_WEB_NGINX_ACCESS_LOG",
    "/mnt/c/Users/user/codex-web/nginx/logs/codex-web-access.log",
))
REQUESTS = Path(os.environ.get(
    "CODEX_WEB_MAINT_REQUESTS_DIR",
    "/mnt/c/ProgramData/CodexMaintenance/requests",
))
STATE = Path(os.environ.get(
    "CODEX_WEB_STATE_DIR",
    str(Path.home() / ".local" / "share" / "codex-web"),
))
AUDIT = STATE / "security-monitor.jsonl"
WINDOW_SECONDS = 600
THRESHOLD = 12
IP_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3}) user=([^ ]+) .*\"[A-Z]+ .*\" 401 ")


def valid_ipv4(ip):
    try:
        return len(ip.split(".")) == 4 and all(0 <= int(piece) <= 255 for piece in ip.split("."))
    except ValueError:
        return False


def audit(event, ip):
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with AUDIT.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"time": time.time(), "event": event, "ip": ip}) + "\n")
    AUDIT.chmod(0o600)


def main():
    seen = defaultdict(deque)
    blocked = set()
    with LOG_PATH.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(0, 2)
        while True:
            line = handle.readline()
            if not line:
                time.sleep(1)
                continue
            match = IP_RE.match(line)
            if not match:
                continue
            ip = match.group(1)
            username = match.group(2)
            if username == "-" or not valid_ipv4(ip) or ip in blocked:
                continue
            now = time.time()
            events = seen[ip]
            events.append(now)
            while events and now - events[0] > WINDOW_SECONDS:
                events.popleft()
            if len(events) < THRESHOLD:
                continue
            request_id = "block_" + ip.replace(".", "_") + "_" + str(int(now))
            payload = {"id": request_id, "operation": "web9443-block-ip", "ip": ip}
            try:
                (REQUESTS / (request_id + ".json")).write_text(json.dumps(payload), encoding="utf-8")
                audit("basic_auth_rate_limit_block_requested", ip)
                blocked.add(ip)
            except OSError:
                audit("basic_auth_rate_limit_block_request_failed", ip)


if __name__ == "__main__":
    main()
