# W4 报告（2026-11-08）

## SUMMARY

0.6.0 版本入口、rot 诊断、发行门禁、功效与交接文档已完成；未创建 git commit。

完成内容：pyproject、nodus/sodus 版本和 OpenAPI 统一为 0.6.0；CHANGELOG 汇总 W0-W3；README 增加 0.6 入口；`sodus debug memory-stats [--json]` 输出重复、近重复占位、矛盾、过期、周增长、lineage、曝光/命中率；新增 temp DB rot 测试；新增 `scripts/release_check.ps1`。

功效报告见 `reports/2026-11-08_ab-power.md`。历史 bench 明确作为非配对输入，推荐 12 题 × 10/20 次的记忆 on/off 配对协议。

验证：`python -m sodus debug memory-stats --json` 成功；版本、CLI、memory staging、rot 测试通过。完整 pytest 与 release_check 的最终结果写入 HANDOFF。
