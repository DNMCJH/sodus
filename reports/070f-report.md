# 070f：模型族软预算、opencode MCP 与并发验证

## 实现

- `budget.soft_families` 支持 claude / gpt / other；文档配置示例为 100 / 80 / 80 CNY，各族缺省或 0 关闭，不隐式启用日硬上限。
- 纯函数 `model_family` 优先使用 run.model，然后 usage_json.attempts[0].model，再按 route / agent 兜底；大小写归一、Claude 关键词和 GPT/o 系列/codex 分支均有测试。
- `spent_by_family` 读取今日 run，通过 ledger.spent 原规则处理成本：CNY 累加、USD 排除、无币种视为 CNY、无成本不估算。
- `check` 返回 family / family_limit / family_spent / family_gated，达到族软上限时 gated；错误包含族、已花费、上限和 --allow-expensive。保留 daily_cny / soft_cny，override 仅豁免软门禁。
- runner 在真实 route、agent 和最终 model（显式参数 > route > profile）解析后、创建 run 前再次检查，auto route 不会因临时未知族误判。
- opencode 默认加入全部宿主，写 mcp.sodus local command 数组；路径支持 --opencode-config > OPENCODE_CONFIG > XDG_CONFIG_HOME > ~/.config。保留 provider 和其它配置，复用原子替换与时间戳备份，支持 check / uninstall / dry-run，坏 JSON 拒写且不输出密钥。
- docs/mcp.md、docs/cli.md 已说明配置、opencode 与四端并发：单 daemon、多线程 HTTP、不同 cwd 并行、同 cwd 写锁、SQLite 单写者。

## 验证

- 定向测试：tests/test_budget.py、tests/test_mcp_install.py、tests/test_state.py 全部通过，退出码 0。
- 族测试覆盖分类、attempts 优先级、混合币种聚合、缺失成本、到线/线下/override、0/off、旧配置和 runner 实际门禁。
- opencode 测试覆盖 temp HOME 生命周期、幂等、备份、无效 JSON、provider/其它 MCP 保留、密钥不打印、三种路径覆盖和原子替换失败。
- in-process ThreadingHTTPServer 上四线程同时 GET /state、POST /tasks、再次 GET /state，断言四个唯一任务落库、响应成功，不启动真实模型。
- 实机 `A:\Anaconda\python.exe -m sodus mcp install --check`：codex/claude/dsh configured，opencode missing；输出包含四宿主，退出码 1 符合尚未安装 opencode 的实际状态。未写入任何真实宿主配置。
- 首轮全量 pytest 发现现有 test_non_loopback_bind_is_refused 使用真实 HOME，与运行中的 Windows daemon.lock 冲突（PermissionError）；已将该测试隔离到 tmp_path，test_server_policy.py 全部通过、退出码 0。
- 最终全量 `A:\Anaconda\python.exe -m pytest -q` 完成至 100%，退出码 **0**。本轮 /state HTTP p95=31.329ms（20 次串行请求，不含预热）。
- `git diff --check` 退出码 0。

## 遗留与边界

软预算按已记账成本检查，不预留未来成本；并发启动及在途 run 可能超线，relay 余额仍是硬约束。用户需显式配置族金额及执行 opencode 安装；本次只修改工作区实现与文档，不改变用户配置。
未 bump 版本，未 git commit / push / merge。

## SUMMARY

完成模型族软预算、opencode MCP installer、四线程 in-process HTTP 并发测试与文档。最终全量 pytest 退出码 0；install --check 包含 opencode，原三宿主保持 configured。未 bump 版本、未创建 git commit；无未完成开发项，实际启用预算与安装 opencode 由用户按文档执行。
