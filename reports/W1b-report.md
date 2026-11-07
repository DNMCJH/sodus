# W1b 交付报告

## 实现

新增 `nodus/episodes.py`、`nodus/dream.py`、`nodus/cli/commands/dream.py` 和 `tests/test_episodes_dream.py`；修改 `nodus/runner.py` 与 `nodus/cli/__init__.py`。未 git commit/push/merge，未改 /ask、lifecycle、DB schema 或其它原有文件，保留开始时已有的未跟踪文件。

runner 挂接点：正常收尾写完 status/usage/provider 后，`_fail` 写完 failed 状态后，以及孤儿 run 清理写完 failed 状态后。每处独立 try/except 并记录 exception 日志，episode 错误不改变 run 状态或向调用方抛出。

verdict：completed + `db.verify_states` 的 ok（来自 verify.green）为 pass；completed + red 为 red；completed 无验证为 unverified；failed/stalled 为 failed。exit=0 不单独产生 pass，非终态不写 episode。沿用现有 verify_states 的 green 优先语义。

幂等：主键固定为 `ep-<run_id>`，INSERT 由 SQLite 主键仲裁并发；只将已存在该主键的 IntegrityError 视作跳过。replay 按当前 workspace、终态、结束时间（无则开始时间）和 id 排序，支持 ISO since 与 limit，返回 scanned/created/skipped/by_verdict；重复回放 created=0。

summary 为确定性文本，不调用模型；evidence_json 保存 run_status、verify、route、provider、cost、currency、相关持久事件、artifact、文件路径及已有 hash。历史回放不读取当前树重新计算历史 hash；没有独立的 verify-time 测试快照时 tests_hash=null，不能宣称完整的测试快照证据已实现。

## CLI 与 dry-run

- `sodus debug replay-episodes [--since ISO] [--limit N] [--json]`
- `sodus dream --dry-run [--limit N] [--json]`
- dream 非 dry-run 明确报未实现，返回 1；两个命令都不依赖 daemon。

dry-run 按当前 workspace 的 episode rowid 读取，默认 50 条，按 route/verdict/cause 分组；只对 pass 组输出最多 5 组确定性候选预览。失败与未验证组保留统计，不生成 lesson 预览。不插入 episode/candidate/batch，不推进游标，不调用模型或写 AGENTS.md。

W1a dream_batch 没有 workspace 列，暂支持 staged/applied 批次的 cursor JSON `{workspace, seq}`；旧数值 cursor 按单 workspace 兼容，失败/计划批次不推进水位。完整批次协议仍由后续工作实现。

## 验证

- 目标命令 `A:\Anaconda\python.exe -m pytest -q tests/test_episodes_dream.py tests/test_memory_staging.py`：退出码 0，27 项通过。
- 测试覆盖 verdict、并发幂等、回放幂等/过滤/隔离、全部表行数不变、分组/游标、CLI JSON/help、runner episode 异常隔离与失败落库。
- `python -m sodus dream --help` 与 `python -m sodus debug replay-episodes --help`：退出码 0。
- `git diff --check`：退出码 0。
- 全量命令 `A:\Anaconda\python.exe -m pytest -q`：退出码 1，唯一失败是 `tests/test_cli_surface.py::test_top_level_help_lists_exactly_the_curated_names` 的顶层命令数量断言（19 != 18），其余通过。该轮运行收集后又追加了一项失败落库测试，追加项已在最终目标测试中通过。

## 遗留

现有 `tests/test_cli_surface.py:81` 硬编码 `len(TOP_LEVEL) == 18`；新 dream 令实际数量成为 19。这与“新增 dream”“全量测试成功”“不修改其它原有文件”同时冲突，已请求允许仅同步此断言，尚未越界修改。

后续接完整 dream 抽取、独立 lineage 证据、人工接受分支、测试快照、L3 marker/漂移/journal/apply/rollback。episode 当前在 runner 收尾定格，后续流水线追加的 verify 事件不会改写已有 episode；需在后续证据协议中明确刷新时机，不能靠重复 replay 覆盖历史。

SUMMARY

W1b 功能已实现，目标 27 项测试退出码 0；全量退出码 1，唯一失败为旧 CLI 数量断言，因文件边界尚未修改它；验收未全部完成，没有提交 git。下一位需获得此单行测试调整的授权，再将 18 同步为 19 并重跑全量测试。
