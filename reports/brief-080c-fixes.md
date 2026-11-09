# 0.8.1-fixes brief：handoff 迁移/check 语义 + 测试网络抖动

## 边界
- 工作区 `A:\VScode\Code\Projects\nodus-0.8.1-fixes`（feat/sodus-0.8.1-fixes，基于 main 98ab57d）。**不 git commit / push / merge**。
- 只修 0.8.0 遗留，不做 plan 执行/handoff 新功能。

## A. handoff 对已提交控制台的支持（最高优先）
1. `handoff sync` 目前要求恰好一对有序 auto 标记，已有手工 `HANDOFF.md` 无法接入。改为：
   - 若控制台已存在且**没有**任何 auto 标记：在 `## 实时状态` 标题后插入一对标记（保留其余字节/CRLF/人工段），然后填入机器段；找不到该节时追加到文件末尾并在 stderr 提示；
   - 若只有一对但不完整/顺序错/多个：仍拒绝并给明确错误；不得静默改坏；
   - 首次迁移要备份原文件（同目录 `.bak-*`）或至少原子写，失败不破坏原文件；测试覆盖无标记/完整迁移/重复标记/缺节。
2. `handoff check`：对“已提交的控制台”不要因为一次正常 commit 就报死。把 `head_mismatch` 降级为 **warning**（尤其是 auto 段 head 是当前 HEAD 的祖先时），`ok`/退出码只看 errors；新增 `warnings` 字段；`daemon_unavailable` 在无 auto 段时应提示 `auto_block_missing`（warning），而不是硬错误；`snapshot_expired` 仍为 warning 或 error 由你定，但文档写清。测试覆盖：HEAD 前进后 check 仍 ok=warnings、真正 daemon 版本不一致仍 error、缺失控制台仍 error。
3. 文档 `docs/cli.md`/`docs/architecture.md` 补迁移步骤：`sodus handoff sync` 首次自动插入标记；check 的 warnings 语义。

## B. 测试网络抖动 10053
- 复现/定位 `tests/test_ask.py::test_http_auth_and_contract`（以及同类 loopback 测试）偶发 `ConnectionAbortedError [WinError 10053]`：优先查测试服务器关闭/客户端提前断开时的 socket 处理；能在测试 helper 里安全吸收就修，不要掩盖产品错误；若根因在产品 server，修 `_is_disconnect`/关闭路径。
- 至少连续跑目标测试 3 轮、全量 1 轮，记录是否仍抖动；不要用重试装饰器掩盖。

## 验收
- 全量 `A:\Anaconda\python.exe -m pytest -q` 退出码 0；
- 新增测试覆盖 A1/A2；B 有根因记录或明确的剩余风险；
- `reports/080c-report.md` 含证据/遗留/SUMMARY。
