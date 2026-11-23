> 产品名 Sodus。

# Sodus 产品原则

名字来自拉丁语 *sodus*：结、节点。多个 agent 在这里交汇、交接、被验收。

一句话：**Sodus 是个人多项目控制塔（双平面架构）：下层协调多 agent 派活、跨模型盲评与可执行验收，上层把各项目状态与待拍板决策投影到用户正在用的 harness。**

## 四个不是

- 不是模型网关（magpie、CC Switch 已经做了）
- 不是 agent 运行时（Claude Code、Codex、DSH 已经做了）
- 不是看板（vibe-kanban 已经做了）
- 不是聊天客户端

## 五个原则

1. **简单**：一个可执行文件、一个数据目录、一份配置。`sodus up` 起来，关掉终端还在跑。
2. **有门槛**：门槛在验收，不在界面。看板谁都能抄，“测试命令 + 证据链 + 交接协议”抄不走。
3. **有封装**：默认配置能跑，不要求用户懂 Python 或 Node。原生安装包和 Docker 镜像二选一。
4. **有接口**：HTTP/OpenAPI、MCP、CLI 三件套；事件 schema 带版本号，可以 replay。
5. **可扩展**：适配器分三类——agent（谁干活）、channel（从哪进来）、provider（模型从哪来）。每类一个 manifest、一个接口，注册即用。

## 四个原语

对外只暴露四个动词，策略挂在动词上，而不是散在一堆功能里：

| 原语 | 共享方式 | 策略 |
|---|---|---|
| **read** | 所有 agent 读同一份快照 | 路径白名单、敏感字段脱敏、记录每个 agent 读了什么 |
| **write** | 同一文件同时只有一个写者 | 写前提案给另一家审，写后看 diff 并跑测试 |
| **terminal** | 沙箱里执行 | 命令白名单、默认断网、超时、输出落 artifact 不进上下文 |
| **run** | 一次带验收的执行 | 先红后绿，门禁只看 exit code 和测试文件是否被改 |

实现可以很复杂，心智模型只有这四个。

## 证据与判决

- 判决只看可执行证据：测试 exit code、测试文件 hash、命令输出。agent 的自述不是判决。
- 审查是建议：换一家 agent 读同一份证据，产出带文件行号的 findings。它能把任务降级为 `needs_review`，不能把失败升级为成功。
- 证据落盘：`runs/<id>/verify-*.txt`、`review.json`、`context.md`、`tasks/<id>/report.md`。换会话、换机器都能复查。
- 一个配置键只有一个写者；一次执行只有一个验收信号。

## 部署形态

| 形态 | 组成 | 适用 |
|---|---|---|
| 单机 | 一台机器，SQLite 本地 | 个人自用 |
| 主从 | Mac mini 常驻控制面 + 台式机 worker，Tailscale 组网 | 多机、长时间任务 |
| 云端 | 控制面进云，channel 走长连接（飞书不需要公网 IP） | 多人、产品期 |

## 非目标（v1 之前）

多租户计费、RBAC、SSO、多区域部署、模型训练。

## 命名过渡

对外叫 **Sodus**：CLI 命令、Python 包、仓库目录、状态目录（`~/.sodus`）都叫 sodus。

- 2026-10-21：旧的 `~/.workbench` 统一到 `~/.sodus`，首次运行自动迁。
- 2026-11：显示名、`sodus` 入口、`SODUS_HOME`/`~/.sodus` 支持落地；目录搬迁和仓库改名还没做，见 [sodus-migration.md](sodus-migration.md)。
