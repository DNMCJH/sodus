# fable / opus 甜区对照（2026-11-10，micro pilot）

任务：`fix-range-parser`、`pricing-discount`（隐藏测试，各 1 次）；配置：fable-med、fable-high、opus-high、opus-xhigh。

| 配置 | solve | 平均时间 | 报告成本 |
|---|---|---|---|
| fable-med | 2/2 | 51s | ¥0.670 + $0.128 |
| fable-high | 2/2 | 97s | $0.470 |
| opus-high | 2/2 | 73s | $0.340 |
| opus-xhigh | 2/2 | 85s | $0.500 |

结论：这 2 道题上四家全过；opus-high 比 fable-high 更快、报告成本更低，fable-med 最快但成本币种混记。没有复现“Fable high 约等于 Opus 5.5 max、费用低一半”的 micro 表现。

限制：n=1/题/配置，成本 CNY/USD 混记且缓存冷启动，不能推翻 FrontierCode 的宏观结果，也不能当成统计结论；只能说在这类小代码任务上 fable 没有明显优势。

之前那组把 opus 放在 max，确实不是它的甜区；这次 opus-high 2/2 且最快/最便宜，说明上一组对 opus 不公平。
