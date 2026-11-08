# W3a 交付报告

## 实现与 schema 映射

继续已有 config/v5 迁移，完成 dream plan → extract → count 与 `sodus memory dream --now [--limit N] [--json]`；`--dry-run` 和原顶层 `dream --dry-run` 保持兼容。未 git commit，未实现 L3 落盘、rollback、审核 API。

设计 episode/candidate/candidate_fts 分别映射到现有 memory_episode/memory_candidate/memory_fts（保留 memory_fts_map）；workspace_id 在 episode/candidate 上名为 workspace，norm_hash 名为 hash，provenance 使用 sources_json。lineage_root 从 episode.evidence_json 读取，缺省回退 task_id/run_id/episode id。dream_batch 保留旧 cursor/finished_at/stats_json，additive 增加 workspace_id、cursor_from/to、output_json、ops_json、memory_version、run_id、target_path、hash_before/after、error、ended_at（started_at 已存在）。新 candidate_evidence 以 candidate_id/episode_id 为主键并有两个查询索引；dream 区间有 workspace_id/cursor_from/cursor_to 唯一索引。旧批次保留，不猜测其 workspace/range；dry-run 保留旧 cursor 兼容读取并结合新 meta 游标。

meta.memory_version 初值 0；dream_cursor:<workspace> 缺省 0。db.SCHEMA_VERSION 从迁移表推导为 5；ask/lifecycle/memory staging 测试统一引用该来源。v5 重连、旧行保留、失败事务回滚均有验证。

## 抽取与证据

生产 extract_model 创建普通 task，通过 RUNNER.start(kind='dream', route_name=config.dream_route()) 执行；禁用验收、记忆注入和历史 handoff，使用临时目录。runner 原有预算、provider、watchdog、usage_json/ledger 路径复用；dream 每日额度按本地日聚合全库 kind=dream 的 usage，含 cache token，plan 和抽取前检查，runner 也兜底检查。routing.dream 缺省回退 routing.default；额度默认 100000 token，0 表示不起批。

fake 边界为 dream.extract_model(eligible_episodes, batch)，测试 monkeypatch 后不调用真实模型；另有 fake RUNNER.start 的适配器测试，确认 route、kind、run_id、usage 持久化和日额度拦截。输出按原字符串保存，重放不调用模型。约定 JSON 为 {"candidates":[{"text":"...","episode_ids":["..."]}]}，也接受候选数组；不合法 JSON 在 count 阶段报错，保留 extracted 输出供排查，不猜测或修补。

只传 pass/人工 accepted_by=human 的 episode；count 再从数据库构造同一资格集合，忽略模型给出的候选 id、其他工作区/区间或不合格 episode。候选按 memory._hash(memory._norm(text)) 去重，低于门槛为 pending，三个不同 lineage_root 且 exposed=0 后为 proposed；同链重试只计一个独立 lineage。injected/eligible 曝光标为 exposed=1，不能自证。既有 harvested 候选不受门槛影响。

## 事务、幂等与边界

plan 用 BEGIN IMMEDIATE，读 meta 游标、取 workspace 内 rowid 区间、插入唯一批次；有未计数的 planned/extracted 批次时直接返回（即使调用者改变 limit），避免重叠。空输入返回 None。extract 已存 output_json 时直接返回，进程内锁避免同进程并发重复抽取；count 在单个事务内完成候选 upsert、episode 证据、FTS、游标推进和 staged 更新。模拟 FTS 写入失败证明证据和游标整体回滚；重放 staged 不重复写入。

本次只完成 DB staging。未对多进程并发抽取提供持久租约；若模型已结束但 output_json 尚未写入即进程崩溃，仍可能再次调用模型，后续如需该窗口 exactly-once 应设计 run 恢复/租约。已持久输出的重放不会重调模型。临时工作目录只提供执行目录隔离，不承诺 harness 沙箱。token 闸遵循已完成 usage 的软上限，不预留并发在跑的 token，不杀运行中任务。

## 测试

- 目标：`A:\Anaconda\python.exe -m pytest -q tests/test_dream_engine.py tests/test_episodes_dream.py tests/test_memory_staging.py tests/test_ask.py tests/test_lifecycle.py --junitxml=reports/W3a-target-tests.xml`，145 项通过，退出码 0；XML 保留结果。
- 新增测试覆盖并发 plan/工作区/区间幂等、资格过滤、extract 重放、count 原子性与游标、独立证据门槛/曝光/自证排除/harvest 豁免、CLI、适配器路由与账本 usage/日 token 限额、v5 重连与迁移失败回滚、数据库迁移/写锁/pytest 超时/watchdog 的 CJK 与两字回退。
- 最终全量：`A:\Anaconda\python.exe -m pytest -q --junitxml=reports/W3a-full-tests.xml`，999 项通过，failures=0/errors=0/skipped=0，退出码 0；XML 保留结果。此前第一轮全量同样退出码 0。
- `git diff --check` 退出码 0；schema 冒烟确认 schema_version=5（目标测试覆盖）。

## SUMMARY

W3a 完成：schema_version=5，目标 145 项与最终全量 999 项测试通过，退出码均为 0；实现、测试 XML 和本报告已产出，未 git commit。下一位接 W3b 的 L3 marker、漂移检测、journal/apply/recovery、rollback 与审核流程；不要将 staged 当作已采纳或已落盘。
