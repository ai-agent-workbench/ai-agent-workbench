#!/usr/bin/env bash
# Codex Web 工作台 —— 环境变量样例
# 用法：按需修改后写入 systemd 单元的 Environment= 行，或 EnvironmentFile 指向的文件。
# 带【必填】的项未设置时应用拒绝启动；其余项均有通用默认值。

# 【必填】登录账号名：TOTP 绑定名称与会话签名标识
export WORKBENCH_ACCOUNT="your-login-name"

# ── 状态与密钥 ──────────────────────────────────────────────
# 状态目录：TOTP 密钥、会话密钥、Windows PIN、审计日志、任务数据、发布快照
# export CODEX_WEB_STATE_DIR="$HOME/.local/share/codex-web"

# ntfy 通知主题名所在的文件（主题名本身是秘密，只落文件、不进代码、不进仓）
# export CODEX_WEB_NTFY_TOPIC_FILE="$HOME/.local/share/codex-web/ntfy-topic.txt"

# ── 文件访问边界 ────────────────────────────────────────────
# 文件管理允许访问的根目录，用 : 分隔多个
# export CODEX_WEB_FILE_ROOTS="$HOME:/mnt/d"
# 禁止网页访问的敏感目录，用 : 分隔多个（默认已含 ~/.ssh、~/.codex、状态目录、应用目录）
# export CODEX_WEB_SENSITIVE_PATHS="$HOME/.ssh:$HOME/.codex"

# ── 功能目录 ────────────────────────────────────────────────
# export CODEX_WEB_INBOX_DIR="$HOME/Inbox"          # 收件箱（手机临时资料）
# export CODEX_WEB_PROJECTS_DIR="$HOME/Projects"    # 项目根目录
# export CODEX_WEB_TOOLS_DIR="$HOME/Tools"          # 共享工具目录（沙盒内只读挂载）

# ── Agent CLI ───────────────────────────────────────────────
# export CODEX_WEB_KIMI_COMMAND="$HOME/.kimi-code/bin/kimi"
# export CODEX_WEB_KIMI_CODE_HOME="$HOME/.kimi-code-web"
# export CODEX_WEB_KIMI_CONFIG_PATH="$HOME/.kimi-code-web/config.toml"

# ── Windows SSH 桥 ──────────────────────────────────────────
# export CODEX_WEB_WINDOWS_KEY="$HOME/.ssh/windows_ed25519"
# export CODEX_WEB_WINDOWS_HOST="user@127.0.0.1"
# export CODEX_WEB_WINDOWS_SSH_PORT="58231"
# export CODEX_WEB_WINDOWS_TASKS='C:\Users\user\codex-web\tasks'
# Windows 侧允许操作的桌面目录（Windows 原生写法 / WSL 视图）
# export CODEX_WEB_WINDOWS_DESKTOP='C:\Users\user\Desktop'
# export CODEX_WEB_WINDOWS_DESKTOP_WSL="/mnt/c/Users/user/Desktop"

# ── 安全监控（security_monitor.py）──────────────────────────
# Windows 网关 nginx access.log 的 WSL 视图路径
# export CODEX_WEB_NGINX_ACCESS_LOG="/mnt/c/Users/user/codex-web/nginx/logs/codex-web-access.log"
# 维护请求投递目录（封 IP 请求写到这里，由 Windows 侧消费）
# export CODEX_WEB_MAINT_REQUESTS_DIR="/mnt/c/ProgramData/CodexMaintenance/requests"

# ── 服务端口 ────────────────────────────────────────────────
# export CODEX_WEB_PORT="18080"

# ── 任务调优（均有默认值，一般无需设置）─────────────────────
# export CODEX_WEB_MAX_RUNTIME_SECONDS="21600"
# export CODEX_WEB_MAX_CONTEXT_CHARS="24000"
# export CODEX_WEB_NO_OUTPUT_WARNING_SECONDS="90"
# export CODEX_WEB_REAPER_GRACE_SECONDS="120"
# export CODEX_WEB_WS_STAGED_MIN_INTERVAL="1.0"
