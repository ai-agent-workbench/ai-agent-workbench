# 架构与安全模型

> 本文是开源脱敏版：所有真实域名、随机前缀、端口、路径均已替换为占位值。

## 数据流

```text
手机浏览器 / PWA（Android Chrome）
  │  HTTPS :<public-port>
  ▼
Windows TLS 网关（nginx on Windows）
  │  · TLS 终止（手机端信任自签 CA 证书）
  │  · HTTP Basic Auth（用户名/密码）+ 失败频率封禁
  │  · 源 IP 限制
  │  · PWA 资源（/manifest.webmanifest、/sw.js、/static/*）免 Basic Auth——
  │    Chrome 必须无认证拿到 manifest 和 Service Worker 才会触发安装提示
  ▼
WSL 内部反向代理（nginx 127.0.0.1:<gateway-port>）
  │  · 仅接受来自 Windows 网关的回环流量
  │  · GeoIP 限制
  │  · 随机前缀入口 /<random-prefix>/ 代理到应用
  ▼
FastAPI 应用（uvicorn 127.0.0.1:18080）
  │  · TOTP（6 位码）验证
  │  · 登录后写签名会话 Cookie：7 天、HttpOnly、Secure、path=/
  │  · SameSite=Lax——不能 Strict，否则 PWA 从桌面图标启动时不携带 Cookie
  ▼
WSL 或 Windows 上的 Codex CLI / Kimi CLI
```

### 手机端入口形态

- **随机前缀入口（主入口）**：`https://<your-domain>:<public-port>/<random-prefix>/`
  由 WSL nginx `location /<random-prefix>/ { proxy_pass http://127.0.0.1:18080/; }` 承载。
- **页面级直接路由**：`/home`、`/login`、`/task/{id}`、`/projects` 等在两层网关上
  单独代理，不带随机前缀也能访问。
- **根路径 `/` 设计为 404**：两层网关均如此配置，应用层登录成功必须重定向到 `/home`，
  不能回到 `/`。

### PWA 与随机前缀的权衡

manifest 与 Service Worker 必须无认证可达，所以随机前缀无法对 PWA 隐藏；
但 Basic Auth 仍然保护所有实际页面路由——前缀只是隐蔽层，不是认证层。

## 安全层次（纵深防御）

1. **接入层**：限制来源并提供 TLS。
2. **Windows TLS 网关**：Basic Auth + 失败频率封禁（security_monitor.py 盯 access.log，
   同 IP 10 分钟内 12 次 401 即投递封禁请求）；PWA 资源单独放行。
3. **WSL nginx**：GeoIP + 来源白名单 + 随机前缀隐蔽层。
4. **应用层**：TOTP、签名会话、高风险操作二次确认、Windows PIN、审计日志。
5. **执行层**：Agent CLI 按任务指定工作目录与权限档运行；
   Kimi 任务再套一层 bwrap 沙盒。
6. **Windows 维护通道**：仅回环 SSH，只允许白名单结构化动作，写操作必须 PIN 审批。

## bwrap 沙盒（Kimi 任务）

- `/usr`、`/lib`、`/lib64`、`/bin`、`/etc` 只读挂载；`/mnt/wsl/resolv.conf` 保留（WSL DNS）。
- 整个 home 目录替换为空 tmpfs，之后只显式暴露：kimi 二进制（只读）、
  `KIMI_CODE_HOME`（可写，供 token 刷新）、任务工作目录（按权限档只读/可写）、
  项目根（只读，跨项目读素材）、共享工具目录（只读）。
- 最后把 home 重新 remount 成只读：任何不在显式白名单内的写入都会失败。
- `~/.ssh` 与交互式 `~/.kimi-code` 凭据**不挂载**——沙盒里的 AI 无法直接 SSH 到 Windows。
- 子进程由常驻单线程 spawn：`bwrap --die-with-parent` 的 PDEATHSIG 绑定 fork 它的线程，
  线程池 worker 空闲回收会误杀任务，因此 spawn 线程与进程同寿命。

## Windows SSH 桥 + PIN 审批闭环

沙盒内的 AI 通过 MCP 工具接触 Windows：

- `windows_inspect`：只读检查（系统概况、Ollama 状态/模型、进程、服务、路径属性、命令探测），
  直接执行。
- `windows_execute`：写操作（拉模型、启停服务、启动程序、建目录、写文本、删除路径）。
  **只创建持久审批请求**，不立即执行；用户在手机上核对摘要、输入 PIN 后才执行。
- `local_execute`：本机维护动作（如重启工作台服务），同样 PIN 审批。

约束：Windows 侧路径白名单（桌面目录 + D:\）、动作白名单、参数正则校验、
PIN 用 PBKDF2-HMAC-SHA256（310k 轮）加盐存储、全程审计落盘。
结果不确定的执行会标记"结果待复核"，重启后恢复检查，不静默假定成功。

## 两阶段发布与冒烟回滚

systemd 单元 `KillMode=control-group`：杀 uvicorn 会连坐杀死同 cgroup 的一切进程
（包括发布脚本和发起任务的 AI 进程）。因此发布拆两阶段：

1. **阶段 1（`scripts/publish-deferred.sh`，任务内执行）**：
   `py_compile` 全部 .py → 创建代码快照 → 写 `pending-publish` 标记 →
   延迟 N 秒 kill uvicorn（让调用方正常收尾；本任务随之中断属预期）。
2. **阶段 2（新进程启动钩子，`app.py` 的 `_verify_pending_publish`）**：
   发现标记 → 等 HTTP 就绪 → 跑 `smoke_test.sh` →
   失败则从**上一份**快照恢复代码并 `SIGKILL` 自己，systemd 用旧代码重新拉起。
   冒烟与回滚在新进程的 cgroup 里执行，不会再被"杀旧 uvicorn"连带。

另有 `release.sh`/`rollback.sh`（有 sudo 的经典流程）与
`scripts/publish-degraded.sh`（无 sudo、服务外场景）。
`publish-degraded.sh` **禁止在任务进程内使用**——原因同上，连坐会丢失冒烟与回滚。

## 数据分类

- 应用源码：可版本化，不含秘密。
- 项目工作区：用户文件与项目记录。
- 任务状态：执行输出、最终消息与任务元数据。
- 安全配置：认证材料、入口路径、CA 与密钥——**禁止进入项目资料与代码仓库**。
- 系统配置：代理、服务、SSH、防火墙与 Windows 计划任务。

## 边界速记

- 文件操作有允许目录白名单（`CODEX_WEB_FILE_ROOTS`）与敏感目录黑名单
  （`CODEX_WEB_SENSITIVE_PATHS`）。
- 高风险任务需要额外确认；全权限档（danger-full-access）启动有独立确认页。
- Windows SSH 只作本机回环维护通道；公网维护 SSH 与网页入口相互独立。
- ntfy 推送只覆盖安全事件（白名单模式）；任务生命周期事件只记审计日志。
