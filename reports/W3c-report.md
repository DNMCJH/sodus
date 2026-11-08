# W3c 交付报告

## 实现

- rollback：`sodus memory rollback --to V --project PATH` 按目标文件和全局 memory_version 顺序重放 `dream_batch.ops_json`，首次历史记录包含原有 marker 内容的 baseline；回放后走 `apply_ops` 新建版本，不删除/改写历史。中间版本恢复逐字测试覆盖，候选 adopted/retired 同步。非法或无历史版本拒绝，marker 漂移拒绝并提示先 reindex。
- apply：读取/漂移检查移入写锁内，统一 journal → staging/fsync → replace → DB 事务；写入 ops、版本、target/hash 与候选状态。旧 harvest/adopt 同样经此路径。recovery 在 hash_after 分支幂等补 DB 事务，不再仅删除 journal。
- daemon：serve 启动 300 秒周期的后台 dream 线程；无 queued/running run 才尝试，meta `dream_last_run:<workspace>` 按本地日期持久去重，失败尝试也占当天次数。先检查 `memory.dream_daily_tokens`，再 plan → extract → count → apply 人已批准的变更；模型仍走既有 `routing.dream`（fallback default）及 kind=dream run/ledger。异常记日志，循环及 daemon 保持运行。
- 看板：暂存候选展示状态、版本和独立未曝光证据数，adopt/reject/edit/retire 调用既有 POST candidates API；409 显示刷新提示。edit 补继承 candidate_evidence。兼容 workspace ID 的 dream 候选与项目路径的旧候选。未新增 HTTP 路由，无需改 OpenAPI。
- 文档：docs/cli.md、docs/mcp.md 说明命令、每日触发/额度、审核入口与排队落盘。

## 验证

- 目标：`A:\Anaconda\python.exe -m pytest -q tests/test_w3c.py tests/test_dream_engine.py tests/test_memory_staging.py tests/test_memory.py tests/test_routes.py tests/test_ui.py --junitxml=reports/W3c-target-tests.xml`：120 tests，0 failures/errors/skips，退出码 0。
- 新增 tests/test_w3c.py：三批后回滚中间版本/再回滚 v1、候选状态与历史保留、漂移不覆盖、CLI、日去重/跨日、额度、busy、异常循环隔离、只落盘人批准候选、看板控件与证据展示。
- tests/test_ui.py 包含 node --check 内联 JS 语法检查；本次未跳过。未实际调用模型、未进行浏览器端交互人工验收。
- 首轮全量：1006 tests，0 failures/errors/skips，退出码 0（reports/W3c-full-tests.xml）。最终代码复跑结果见下方 SUMMARY。

## 边界与后续

- 每日去重包含失败；故障修好后可用手动 dream，或等次日自动触发。当天自动 dream 后才批准的变更等下一次日任务落盘。
- 历史只可回放自记录开始的版本；不伪造旧版缺失的 ops。`reindex` 沿用已有实现，此次不扩展为全量文件索引重建。
- 未 git commit/push/merge；未改动既有无关未跟踪材料。下一位可接 W4/W5 的剩余发行门槛及真实模型/浏览器集成验收。

## SUMMARY

最终全量：首轮 1006 tests、0 failures/errors/skips、退出码 0（reports/W3c-full-tests.xml）；收尾复跑 verify green exit=0。worker 进程以 exit=1 结束属 codex CLI 偶发退出（usage 0 token），不是测试失败。
