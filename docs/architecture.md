# Nodus 架构

> 版本 0.2.0 · 2026-10-20 · 这份文档解释「为什么代码长这样」，README 解释「怎么用」。

## 1. 需求（先定边界）

Nodus 是**任务验收层**：它不训练模型、不代理请求、不写 agent runtime，只回答一个问题——这个任务算不算完成。

| 角色 | 动作 | 系统里的落点 |
|---|---|---|
| 派活的人 | 写目标 + 验收标准，指定或让 Jev 选 agent | `task` → `run` |
| 干活的 agent | 读任务、改代码、留下交接 | `runner` + `handoff.md` |
| 验收的人 | 看 exit code、看证据路径、看审查结论 | `verify` / `review` / `report` |
| 下一个会话 | 读 brief 或 resume，接着干 | `dsh_sessions` + `brief` / `resume` |

四条硬规则，写在代码里而不是文档里：

1. **验收只认可执行证据**：测试 exit code + 测试文件 hash，agent 自述不算。
2. **审查只降级不升级**：review 可以把 `done` 变成 `needs_review`，不能把失败变成成功。
3. **跨会话只读 + 显式续写**：会话文件只读；续写要过锁、过 cwd 校验、过白名单。
4. **单机单用户优先**：SQLite、环回监听、一个 token；多租户只留 workspace_id 接缝。

## 2. 分层

```
nodus/cli/            argparse + 每个命令一个 handler（不碰业务）
  client.py        HTTP 客户端：唯一知道 daemon 地址的地方
nodus/server.py       HTTP + SSE，状态所有者
nodus/tdd.py          流水线：红→绿→（可选）审查
nodus/review.py       跨 agent 审查：证据收集 + 结构化 findings
nodus/fix.py          修复回路：审查→修→验→复审
nodus/report.py       任务报告（只读聚合）
nodus/runner.py       进程生命周期：spawn、读流、落事件、交接单
nodus/agents.py       agent 注册表：vendor 的 argv/env/能力
nodus/adapters.py     各家 JSONL → 统一事件（纯函数）
nodus/verify.py       测试检测与执行（纯函数 + 落盘）
nodus/dsh_sessions.py DSH 会话读取 / brief / 锁（只读为主）
nodus/db.py           SQLite：所有读取都按 workspace 过滤
nodus/config.py       路径、token、secrets、用户配置
```

依赖方向是单向的：`config ← agents ← runner ← {tdd, review, fix, report} ← {cli, server}`；`adapters` 和 `verify` 不依赖上层；`db` 是基础设施，被所有层使用，但不反向 import 业务。

### 为什么 CLI 不直接 import 业务

`nodus run` 走 HTTP，`nodus tdd` 等流水线在 CLI 进程里执行（见 ADR-004）。两种路径都只通过 `db` 落状态，所以 `nodus watch` 从另一个进程也能看到。代价写在 ADR 里，不藏。

## 3. 扩展点

| 想加什么 | 改哪里 | 不用改 |
|---|---|---|
| 新 agent（Gemini CLI 等） | `agents.py` 加一个 `AgentSpec`；`adapters.py` 加一个 parser | runner、CLI、db |
| 新后端（同一 CLI 换 provider） | 只在 `agents.py` 加一个 `AgentSpec`：`vendor` 指向原 CLI，`env_secrets` 给凭据，`pricing` 给价目 | parser、runner、db |
| 新命令 | `nodus/cli/commands/<module>.py` 里 `add(sub)` + 一个 handler | 其他命令 |
| 新事件类型 | `adapters.py` 返回新的 `type`；消费方按前缀过滤 | schema（event 是 JSON） |
| 新存储后端 | 按 `db.py` 的函数面重写；调用方只认函数名 | 业务层 |
| 新通知渠道 | 目前没有；加的时候挂在 event 流的订阅侧 | 核心 |

## 4. 安全模型

| 面 | 措施 | 位置 |
|---|---|---|
| 网络 | 只绑 127.0.0.1；非环回要 `NODUS_ALLOW_REMOTE=1` | `server.serve` |
| 认证 | Bearer token，`hmac.compare_digest` 常量时间比较 | `server.authorized` |
| 请求体 | 上限 1 MB，超了 413 | `server.body_allowed` |
| 密钥 | `~/.nodus/secrets.json`，per-workspace 覆盖；ACL 去掉继承（仅当前用户 + SYSTEM）；只进入子进程环境，不打印 | `config.load_secrets` |
| 凭据注入 | `AgentSpec.env_secrets` 声明 secret→env 映射，运行时注入；agent 配置目录里不存 token | `agents.build_env` |
| 数据隔离 | 每张表带 `workspace_id`，所有读操作过滤 | `db.py` |
| 会话读取 | 可选 `session_roots` 白名单；默认不限制但每次显式指定会话 | `dsh_sessions._allowed` |
| 会话写入 | 单写者文件锁 + 会话 cwd 校验 + 120s 新鲜度守卫 | `dsh_sessions.session_lock` |
| 进程 | agent 以 `bypassPermissions` 跑，cwd 是唯一边界 | `agents.argv` |

已知缺口（不粉饰）：

- Windows 上 token 文件没有 ACL 加固，`chmod` 是 no-op。
- 没有文件读取审计；review 读哪些文件只记录在 `context.md`。
- 会话锁是建议性的：绕过 Nodus 直接跑 `dsh --session-id` 不会看这把锁。
- agent 能执行任意命令，这是产品定位决定的，不是遗漏。

## 5. ADR（架构决策记录）

**ADR-001 SQLite + WAL，单写者。** 单机单用户，状态量小，事务简单；WAL 让读不阻塞写。并发上限是真实的：10 个以上同时写没测过。

**ADR-002 文件交接，不共享对话。** 跨 agent 传的是 handoff.md（确定性模板）+ artifact，不传聊天记录。好处是上下文可预算、可审计；坏处是长任务需要人补「未决」。

**ADR-003 审查是建议，不是判决。** review 的 verdict 只影响任务状态降级和是否触发 fix；它不能替代测试。理由：LLM 审查会误报，也会漏报，门禁必须可执行。

**ADR-004 流水线在 CLI 进程执行。** `nodus tdd/review/fix` 直接在命令行进程里起 agent，不经过 daemon 队列。好处是终端能实时流、Ctrl-C 即停；坏处是关终端会中断流水线，且 `nodus stop` 管不到别的进程里的 run。迁移到 daemon 作业队列是后续工作，不是当前缺陷清单之外的隐藏项。

**ADR-005 会话桥只读 + 显式续写。** DSH 没有会话间消息通道；往 append-only 存储里注入会破坏 GUI 的 live state。所以读走文件、写走 `--session-id` 新 turn，并要求同 cwd + 单写者。

**ADR-006 一个 vendor = 一个 spec + 一个 parser。** 除这两处外，代码里不出现 vendor 名字（报告里的成本说明除外）。

**ADR-007 后端可换，壳不用换。** MiniMax 提供 Anthropic 兼容端点，Claude Code 的 stream-json 契约不变，所以它的 `vendor` 字段就是 `claude`——同一个 CLI、同一个 parser、同一个 runner，差别只在 base URL、凭据和价目。加它只动了 `agents.py` 一个文件、没新增 parser，这条 ADR 顺带验证了 ADR-006 的边界：`vendor` 表示"哪种流格式"，不表示"哪家公司"。代价：`~/.claude/settings.json` 的 `env` 块优先级高于进程环境变量，换后端必须换 `CLAUDE_CONFIG_DIR`，否则请求静默打回原来的代理（实测报 401 并退避重试 10 次）。

**ADR-008 成本读表，不读自述。** 成本有三个来源，优先级 metered > list_price > reported。metered 是在 run 前后读 provider 计费接口求差（中转站累计消耗、DeepSeek 余额）；list_price 是 token × 公开价目，只有确认对方按价目扣费时才等价于实付；reported 是 CLI 自己算的账，只做兜底。
理由：CLI 的价目表是别人的（Claude Code 用 Anthropic 价格给任意后端计价），中转站还会叠自己的分组倍率，两者都推不出来，只能读表。实测同一个 run：CLI 自报 0.2627，实付 0.3230（站内余额），比值 1.23；换一次模型组合变成 1.44。
代价：每个 run 多一次前置 HTTP 和最多 4s 的落账等待；并发 run 的窗口会重叠，`meter.window_s` 记录窗口但不消除重叠。中转站"站内余额"到人民币的换算比例是账户事实，API 不暴露，放在 `config.json` 的 `cost_rates` 里。

## 6. 质量门槛

- `python -m pytest -q`：91 条，覆盖解析、策略、隔离、锁、报告、计费表。
- `scripts/smoke_cli.ps1`：29 个命令逐个 `--help` + 关键命令真实执行。
- 每个新能力都要有一次真实调用记录（test/tdd/review/fix/report 都有）。

## 7. 还没做的

- daemon 作业队列（ADR-004 的迁移）。
- 打包（PyInstaller / Go）。
- UI / SSE 客户端。
- 多租户鉴权（现在只有 workspace 字段）。
