# W1c report — 2026-11-08

## 实现

- `nodus/lifecycle.py` 增加纯函数 `task_state(task, runs, reviews, verify)`：选最新 work run；无 run→open，queued/running→running，stalled→stalled，failed/red→failed，pass + 非 accept 审查→needs_review，pass + 无审查/accept→done，未验证→unverified。人工 accepted_by 不升级为 done，也不在纯函数中写数据库。
- verify 输入接受设计词汇 pass/red/unverified/failed，也兼容 `db.verify_states` 的 ok/none；支持按 run id 传 verdict，审查按 target_run 与最新 ts 筛选。
- `ask_state(run, promoted_task_id, expires_at, now)`：expired 优先于 promoted，其余保持 queued/running/stalled/failed，completed→answered；严格使用 now > expires_at，函数不读时钟。未实现 /ask 端点。
- 校准 stalled writer 注释：实际由 watchdog.mark_stalled 写入。

## 迁移

- 在既有版本化迁移框架追加 v3：新增 `task.accepted_by TEXT`，允许 NULL 或 human；PRAGMA 检查列后再 ALTER，重复连接幂等。
- 新库与 v1/v2 库均到 schema_version=3；保留原 title、acceptance 与记录，人工确认不复用 acceptance。
- `tests/test_memory_staging.py` 仅将两条当前 schema 版本契约断言从 2 更新为 3。

## 读写方

- 搜索全部 task.status 写入：仅 tdd.py、fix.py 的流水线写方需要迁移，均改为 task_state 返回值后写回。
- lineage 的 ok/unverified 桶使用同一派生函数；原有 needs_attention/recover gate 不变。
- fix 复审虽指向原始目标 run，结论描述修复后的工作树，因此适用最新 fix run；修复后红测现在按设计为 failed。
- 新增逐行映射、ask 优先级/时间边界、最新 work/审查选择、新库/旧库/重复连接/人工字段、lineage 对照、TDD/fix 流水线对照测试。

## 验证

- `A:\Anaconda\python.exe -m pytest -q tests/test_lifecycle.py tests/test_memory_staging.py tests/test_episodes_dream.py`：123 条通过，退出码 0。
- `A:\Anaconda\python.exe -m pytest -q`：954 条通过，退出码 0。
- `compileall`（修改的 Python 文件）与 `git diff --check`：退出码 0。
- 首轮新增迁移测试的旧库夹具缺少 session_id，已修正为真实 task 表结构；以上为修正后的最终验证结果。

## 遗留与交接

- 无 /ask、promote 或人工确认 API，本次只提供 W2 可复用的派生函数及 accepted_by 存储。
- W2 调用 ask_state 时传入当前时钟、过期时间与 promoted_task_id；人工确认只写 accepted_by=human，不能写 done。
- 未 git commit/push/merge；未修改其他 worktree。原有未跟踪的报告、评审与日志保持原样。

## SUMMARY

W1c 完成：派生映射、schema v3 accepted_by 迁移与读写方对齐均已实现；目标 123 条、全量 954 条测试通过，退出码均为 0。下一位接 W2 入口与幂等实现；未提交 Git。

