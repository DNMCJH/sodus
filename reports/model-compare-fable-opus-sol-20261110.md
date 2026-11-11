# fable 5.2 / opus 5.5 max / sol 对照（2026-11-10，pilot）

## 结论

这组 pilot 不支持“Fable 5.2 high 明显强于 Opus 5.5 max”的说法；质量上 **fable-high 4/4、fable-xhigh 4/4、sol 4/4、opus-max 3/4（pricing-discount 超时）**。样本只有 4 道题、每题 1 次，不能当作统计结论；成本因 CNY/USD 混记，无法直接证明“半价”。

## 设计

- 任务：仓库自带 `eval/tasks` 里 4 道隐藏测试题：fix-range-parser、pricing-discount、unify-slugs、word-metrics。
- 判定：hidden tests 全绿才算 solve；超时/agent_error 算失败。
- 配置：fable-high（claude-fable-5-2, effort high）、fable-xhigh（effort xhigh）、opus-max（claude-opus-5-5, effort max）、sol（codex/gpt-6.1-sol，默认 effort）。
- 每题每配置 1 次，共 16 次；没有重复，不能算置信区间。

## 结果

| 配置 | solve | 平均 solve 时间 | 报告成本（CNY + USD） |
|---|---|---|---|
| fable-high | 4/4 | 76.5s | ¥0.4827 + $0.5402 |
| fable-xhigh | 4/4 | 91.1s | ¥0.7690 + $0.5901 |
| opus-max | 3/4 | 118.2s | ¥1.9464 + $0.3817 |
| sol | 4/4 | 83.9s | 未记账 |

- 单题：fable-high 全部 solve（pricing 144.4s 最慢）；opus-max 的 pricing-discount 在 306.4s 超时；fable-xhigh 全部 solve 但更慢。
- fable-xhigh 未体现“超越所有模型”的优势；fable-high 至少不差于 opus-max。
- 若粗暴按 1 USD=1 CNY 相加，fable-high≈1.02、opus≈2.33；但这是错的口径，不能作为“半价”证据。
- 本次对比中转余额从约 ¥86.87 降到 ¥68.95（约 ¥17.9，含重跑/基础设施错误）。

## 无效数据

- 之前自建的 2 道题 pilot（LRU/merge）中，LRU 测试本身写错了（`get` 后应把 key 变最近使用，却期望它被淘汰），所以 fable/opus 的 LRU 失败不代表模型能力；该 pilot 不纳入结论。

## 基础设施问题

- `claude` 命令的 npm shim 缺失，导致 fable-xhigh/opus-max 各有一到两个任务 `command not found: claude`；已恢复 shim（指向 Claude Code 扩展的 native binary），重跑后这些 cell 完成。
- 重跑：fable-xhigh word-metrics、opus-max unify-slugs/word-metrics；opus pricing-discount 超时保留为失败。
- 成本币种混记（CNY/USD）是当前 relay 记账问题；sol 的 relay-openai 成本未记账。

## 建议

要下“fable 是否比 opus 更便宜/更好”的结论，需要预注册的重复实验：至少 8 道不同难度题 × 每配置 3 次（24 对），统一定价口径，记录 solve、总成本、墙钟时间，并做配对区间；这组 4×1 只能当 smoke。
