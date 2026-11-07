# W1a 交付报告

## 改动

仅修改 `nodus/db.py`，新增 `tests/test_memory_staging.py` 和本报告；未改 CLI，未提交 git。开始时已有的 `_reviews/`、`ops_monitor.log`、`reports/OPS_STATUS.md` 和两份 brief 保持原样。

按 W1a brief 的具体契约实现 `memory_episode`、`memory_candidate`、`memory_exposure`、`dream_batch`、`memory_fts_map`，以及 run、workspace/status、hash、candidate_id 索引和 workspace/hash 唯一索引。表名和字段以 W1a brief 为准，与设计 §4.4 的完整草案有意保留阶段差异。

DB API：追加 episode、按 rowid 独占水位读取 episode（返回 `seq`）、候选插入/查询/CAS 更新、曝光追加和可选 workspace 的检索。候选默认版本 1；CAS 成功递增版本，缺失或过期版本抛 ValueError；文本修改同步更新 hash 与 FTS。hash 未提供时计算文本 SHA-256。插入用写事务与唯一索引保证同 workspace/hash 并发去重。JSON 字段使用序列化字符串。

FTS 优先使用 trigram；不可用时记录 logging 警告并尝试 unicode61；FTS5 整体不可用时仍可使用 LIKE。少于三字、FTS 无匹配或不可用时走转义后的字面子串 LIKE，覆盖「接口」等两字中文查询。索引映射与候选写入在同一事务内更新。

## 迁移幂等性

新增版本迁移列表 `_MIGRATIONS` 与 `_migrate_v2`；旧 schema 补列与旧 memory 备份流程保留。基础 DDL 全部 IF NOT EXISTS；v2 迁移通过 BEGIN IMMEDIATE 串行化，重新读取版本，在事务内建 FTS 并更新 schema_version=2，失败不标记缓存已就绪。新库直接升级到 2；已有 v1 数据不删除、不覆盖；重复连接或清空进程缓存再连接均不会重复建表或丢失候选。更高版本号不被降回 2。

## 测试

- `A:\Anaconda\python.exe -m pytest -q tests/test_memory_staging.py`：退出码 0，13 项通过。
- 新测试覆盖 schema、清缓存重复连接、模拟 v1 升级保留旧 task、episode 水位、并发 hash 去重、workspace 隔离、CAS 与 FTS 更新、曝光、中文/中英混合/英文检索、无 FTS 的 LIKE、trigram 不可用时 unicode61 与警告。
- `A:\Anaconda\python.exe -m pytest -q`：退出码 0，全量 909 项通过（另以 collect-only 确认总数）。
- `git diff --check`：退出码 0，无空白错误。

## 遗留与交接

后续 W1b/W2 使用上述 DB API；dream 批次执行、证据门槛、candidate_evidence、L3 marker/漂移检测、journal/回滚、入口及 CLI 均不在本次实现范围。检索本身不自动记录曝光，调用方需携带 task/run/channel 调用 memory_record_exposure；曝光为追加记录，不以 hits 代替证据。dream_batch 本次仅提供 brief 指定的表结构，后续完整协议若需设计草案的额外字段，应做 additive 扩展。

SUMMARY

W1a 完成：schema v2、DB API、幂等迁移与 13 项新增测试已交付；全量 909 项测试通过，退出码 0；仅改动 db.py、新测试和本报告，未改 CLI、未 git commit。
