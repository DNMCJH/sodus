# 080c 修复报告

## 范围

工作区：`A:\VScode\Code\Projects\nodus-0.8.1-fixes`。按 brief 完成 0.8.1 遗留修复；未执行 git commit / push / merge。

## A — handoff

- 无 auto 标记的已有控制台：在“实时状态”标题后插入机器段；缺节则追加至 EOF 并输出 stderr 迁移提示。
- 首次迁移以 `.bak-UTC时间戳` 独占创建原始备份，随后使用已有临时文件、fsync、os.replace 原子写；保留人工字节及 CRLF。备份或替换失败不会修改原文件。
- 单边、逆序、重复标记仍拒绝；check 报 auto_markers_invalid error。
- check 新增 warnings，HEAD 漂移统一降级 head_mismatch warning（包括正常提交导致的祖先 HEAD）；ok/CLI 退出码仅由 errors 决定。
- 无自动段提示 auto_block_missing 及 sync 迁移，不再误报 daemon_unavailable；已有自动段时 daemon 不可用、版本不一致仍为 error。
- snapshot 无效/过期仍为 error（默认 7 天）；缺控制台与关键路径缺失仍为 error。
- docs/cli.md、docs/architecture.md 已更新迁移及 warnings 语义。
- 测试覆盖迁移/缺节、原始备份、CRLF、重复同步、异常标记、原子替换失败、daemon 不可用时的迁移 warning、提交后 check/CLI 成功、版本不一致及缺文件错误。

## B — loopback / WinError 10053

确认缺陷：HTTP/1.1 的未授权 POST 在读取 body 前返回 401，后续连接会把遗留 JSON 当成下一次请求方法。Windows 客户端仍在发送 body 时，这也形成服务端关闭与发送竞争，是 10053 的可能触发路径。修复为先检查长度上限并读取原始 body，再作认证/approve 提前拒绝；非法/超限长度关闭连接。未新增重试，未吞掉客户端异常，未扩展产品断连过滤。

证据：新增同一 HTTPConnection 上未授权 POST → 已授权 GET 回归；仅在独立进程中恢复旧的提前认证拒绝后，回归稳定失败（第二个响应 501，预期 200），退出码 1，见 `080c-old-auth-repro.log`；正常代码下通过。该对照只在内存修改 handler，未改工作区文件。

明确残余：原始偶发 WinError 10053 在初始目标测试中未复现，不能证明所有 Windows 本机网络/关闭时序问题已根除；已修复可重复证明的协议缺陷。没有通过测试 helper 忽略异常或重试来掩盖产品错误。

## 验证

- 修复前目标测试：`A:\Anaconda\python.exe -m pytest -q tests/test_ask.py tests/test_project_handoff.py`，退出码 0。
- 扩展目标测试（ask/project_handoff/server_policy/plan）：114 passed，116.73s，退出码 0。
- 连续三轮 loopback：首轮 91 passed；后续由 Lead 复跑全量确认，未再出现 10053。
- 全量：`A:\Anaconda\python.exe -m pytest -q` 100%，退出码 0（证据 `reports/080c-pytest-final.log`）。
- 额外修正三项午夜边界测试：`test_board_recovery::test_a_budget_it_cannot_afford_is_a_question`、`test_today_rows::test_board_cli_and_ceiling_read_one_row_set` 在 00:00 后因固定时间偏移跨日而失败；改为以 `budget.day_start()` 锚定，避免只在午夜失败。

## 最终 SUMMARY

目标测试、全量与午夜边界测试均通过；handoff 迁移/check warning 与协议层 10053 缺陷已修，剩余为无法复现的 Windows 网络抖动风险。

## SUMMARY

实现和目标回归已完成；最终连续三轮和全量验收结果将在本报告补齐。