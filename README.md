# Codex Web 工作台（AI Agent Workbench）

> **免责声明**：本项目按作者个人环境（WSL + Windows 单机）深度定制，开源仅供参考与思路借鉴。
> 作者**不承诺维护、适配或答疑支持**；部署到自己的环境需要你自行理解每一处配置与安全边界。
> 把一套能远程指挥 AI 写代码的系统暴露到公网是有真实风险的，请在完全理解安全模型后再动手。

一个自托管的"AI 工作台"：FastAPI 写的 Web 控制台，让你在**手机浏览器（PWA）上远程指挥
Codex CLI / Kimi CLI 编程 Agent**——发起任务、追问、审批高风险操作、浏览和编辑文件、
管理项目，全部在被窝里完成。

> **English**: A self-hosted web console (FastAPI + PWA) for remote-controlling AI coding
> agents (Codex CLI / Kimi CLI) from your phone. Features a PIN-gated approval workflow for
> high-risk operations, bubblewrap sandboxing, layered nginx gateway security (Basic Auth,
> GeoIP, TOTP), full audit logging, and a two-stage release pipeline with smoke-test rollback.
> Notably, every line of code was written by AI — the author makes decisions, AI implements.
> Shared as a reference implementation; see `docs/` for the architecture and the
> human-AI collaboration charter.

## 它是怎么来的

这个项目本身也是一个方法论实验：**作者完全不懂编译、不写代码，只负责出决策和验收；
全部代码由 AI（Codex / Kimi）编写**。仓库里的每一行 Python、每一个 shell 脚本、每一页
前端，都是"人说目标、AI 实现、人验收"循环的产物。`docs/ai-collaboration-charter.md`
收录了支撑这种开发方式的 AI 协作章程（确认流程、发布纪律、工作记录格式），
这是本项目自认为最稀缺的内容——代码可以重写，方法论不行。

**背后的故事（中文长文）**：从 6·30 装机杀毒夜到 7·31 写爆 D 盘停机七小时的完整复盘——
掘金[《AI 给自己写了本"员工手册"，我负责签字，它负责写代码和上线》](https://juejin.cn/post/7669635386159415336)｜
知乎[《一行代码都不会的我，给 AI 当了一回"架构师"：它写代码，我立规矩》](https://zhuanlan.zhihu.com/p/2067768204131358015)。
*The full story behind this project (in Chinese): [Juejin](https://juejin.cn/post/7669635386159415336) ｜ [Zhihu](https://zhuanlan.zhihu.com/p/2067768204131358015).*

## 功能一览

- **双 Agent 后端**：Codex CLI（经模型代理）与 Kimi CLI（直连），任务级切换
- **PWA 手机端**：可安装到桌面，暗色界面，WebSocket 准实时进度
- **任务生命周期**：发起 → 确认页 → 执行 → 追问/分叉 → 项目归档；草稿、排队轮、懒收割
- **文件管理**：浏览器内上传/下载/编辑/复制/移动/删除，严格的路径白名单与敏感目录黑名单
- **Windows SSH 桥 + PIN 审批闭环**：沙盒里的 AI 想动 Windows（装模型、启停服务、写文件）
  只能提交结构化审批请求，你在手机上输入 PIN 后才执行；全程审计
- **bwrap 沙盒**：Kimi 任务运行在 bubblewrap 里——home 目录换成只读 tmpfs，
  只暴露工作目录与 kimi 自身配置，SSH 密钥与交互式凭据不挂载
- **两阶段发布 + 冒烟回滚**：发布脚本只负责打快照、写标记、杀掉旧进程；
  新进程启动钩子跑 `smoke_test.sh`，失败自动从上一份快照回滚并自杀，
  systemd 用旧代码重新拉起
- **安全监控**：Basic Auth 失败频率监控，超限自动请求网关封 IP；
  ntfy 推送仅覆盖安全事件（白名单模式）

## 架构示意

```text
手机浏览器 / PWA
  │  HTTPS（自签 CA）
  ▼
Windows TLS 网关（nginx on Windows）
  │  TLS 终止 + HTTP Basic Auth + 源 IP 限制 + 失败封禁
  │  PWA 资源（/manifest.webmanifest、/sw.js、/static/*）免 Basic Auth
  ▼
WSL 内部 nginx（127.0.0.1:<gateway-port>）
  │  GeoIP 限制 + 随机前缀入口 + 仅接受网关回环流量
  ▼
FastAPI 应用（uvicorn 127.0.0.1:18080）
  │  TOTP 6 位码 → 签名会话 Cookie（7 天，HttpOnly/Secure）
  ├──→ routes/       页面路由：任务、文件、项目、审批、设置
  ├──→ tasks.py      任务生命周期：spawn CLI、收割输出、审批闭环
  ├──→ engine/       Codex exec 适配 / Kimi ACP 会话适配
  └──→ windows_bridge.py   WSL→Windows SSH 桥（MCP 工具 + PIN 审批）
         │
         ├──→ codex exec（bwrap/内置沙盒 + STDIO MCP 桥）
         └──→ kimi CLI（bwrap 沙盒 + HTTP MCP 桥）
```

安全层次（纵深防御，任何单层被破都不致命）：

1. 接入层：TLS + 来源限制
2. 网关层：Basic Auth + 失败频率封禁（security_monitor.py）
3. 内部反代：GeoIP + 随机前缀 + 仅回环
4. 应用层：TOTP、会话签名、高风险确认、Windows PIN、审计日志
5. 执行层：bwrap 沙盒 + 权限档（read-only / workspace-write / danger-full-access）
6. 维护通道：Windows SSH 仅回环，只允许白名单结构化动作

## 快速上手

### 依赖

- Python 3.11+（用到 `tomllib`）
- `pip install -r requirements.txt`（建议 venv）
- 系统命令：`bubblewrap`（bwrap）、`ssh`、`curl`
- Agent CLI：Codex CLI 和/或 Kimi CLI 已安装并完成登录
- 可选：Windows 侧 OpenSSH Server（Windows 桥）、nginx（网关）、ntfy.sh 账号（通知）

### 环境变量

所有个人化配置都走环境变量注入，代码里没有任何硬编码的账号或私人路径。
完整样例见 [`config.example.sh`](config.example.sh)，核心几项：

| 变量 | 必填 | 说明 |
|---|---|---|
| `WORKBENCH_ACCOUNT` | **是** | 登录账号名（TOTP 绑定与会话签名使用），未设置应用拒绝启动 |
| `CODEX_WEB_STATE_DIR` | 否 | 状态目录（TOTP 密钥、会话密钥、PIN、审计日志运行时生成于此），默认 `~/.local/share/codex-web` |
| `CODEX_WEB_FILE_ROOTS` | 否 | 文件管理允许访问的根目录，`:` 分隔多个；默认仅 `$HOME` |
| `CODEX_WEB_SENSITIVE_PATHS` | 否 | 禁止网页访问的敏感目录，`:` 分隔；默认 `~/.ssh`、`~/.codex`、状态目录、应用目录 |
| `CODEX_WEB_INBOX_DIR` | 否 | 收件箱目录（手机上传的临时资料），默认 `~/Inbox` |
| `CODEX_WEB_PROJECTS_DIR` | 否 | 项目根目录，默认 `~/Projects` |
| `CODEX_WEB_WINDOWS_*` | 否 | Windows SSH 桥的密钥/主机/端口/任务目录/桌面白名单 |
| `CODEX_WEB_NTFY_TOPIC_FILE` | 否 | ntfy 主题名文件（主题名是秘密，只落文件不进代码） |

### 运行

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export WORKBENCH_ACCOUNT=your-login-name
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 18080 --proxy-headers
```

首次访问 `/setup` 会生成 TOTP 密钥并显示二维码，用验证器（Aegis 等）扫码绑定后即完成初始化。
TOTP 密钥、会话签名密钥、Windows PIN 全部在运行时生成于状态目录，代码仓库不含任何秘密。

### systemd 部署

仓库附带三个单元样例（路径与 `User=` 均为 `/home/user` 占位，**部署前必须改成你的实际值**）：

- `codex-web.service` — 主服务
- `codex-web-security-monitor.service` — Basic Auth 失败封禁监控
- `scripts/codex-web-healthcheck.service` + `.timer` — 每 5 分钟健康探活 + 磁盘预警（ntfy 告警）

发布与回滚：

- `release.sh` / `rollback.sh` — 有 sudo 的经典发布/回滚
- `scripts/publish-deferred.sh` — 任务进程内唯一允许的两阶段发布
  （systemd `KillMode=control-group` 下从 cgroup 内"逃生"的方案，详见 `docs/architecture.md`）
- `scripts/publish-degraded.sh` — 无 sudo 时在服务外（如 SSH 会话）使用

## 文档

- [`docs/architecture.md`](docs/architecture.md) — 架构与安全模型详解
- [`docs/ai-collaboration-charter.md`](docs/ai-collaboration-charter.md) — AI 协作章程（方法论精选）

## License

MIT，见 [LICENSE](LICENSE)。
