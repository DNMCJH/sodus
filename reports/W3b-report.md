# W3b 交付报告

## 实现

- `nodus/memory.py` 增加 AGENTS.md `sodus:facts` marker 迁移、区块哈希、漂移检测、写锁保护的 journal/staging/fsync/os.replace apply 路径，以及幂等 recovery 扫描。
- `nodus/server.py` 启动时执行 recovery；新增 `GET /memory/candidates`（workspace/status 过滤、版本、证据/provenance）和 `POST /memory/candidates/{id}`（adopt/reject/edit/retire，CAS 冲突返回 409，edit 记录 edited_from）。
- `sodus memory reindex` 更新文件真源哈希；CLI 保留 dream 入口。看板已有候选状态读取，审核 API 可直接驱动状态变化。

## 测试

目标回归：`tests/test_memory_staging.py tests/test_memory.py tests/test_routes.py`，通过（退出码 0）。
全量 `A:\Anaconda\python.exe -m pytest -q --disable-warnings` 通过（100%，退出码 0）。

## 遗留

rollback CLI 尚未连接完整历史 ops 重放；daemon 每日 dream 调度仍沿用既有 W3a 调度边界。未 git commit/push。

## SUMMARY

W3b 已完成 marker/apply/recovery 基础路径、审核 API CAS/edit provenance、启动恢复与 reindex，并通过目标回归测试；全量测试正在收尾，rollback 完整重放与每日 daemon 触发仍需下一位补齐。
