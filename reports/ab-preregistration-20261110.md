# 效率 A/B 预注册（sodus 0.8.x）

日期：2026-11-10（用户授权 goal 模式期间起草；未开始跑）

## 问题

同样的任务，经 sodus 派活（planning/worker/verify/review）是否比直接调 agent 更贵、更慢或质量更差？

## 主要指标

- 主指标：任务验收通过率（隐藏测试/verify ok），二分类。
- 次指标：墙钟时间、总成本（含 planning/review/retry/verify）、人工介入次数、返工次数、首次可用时间。

## 设计（预注册，不事后改）

- 12 道中等难度任务，避开现有满分题（天花板）。
- 每题两组：A=直接 agent（同一模型/预算，临时干净目录和 HOME）；B=sodus 派活（同模型，含 planner/verify/review）。
- 每组每题 10 次，配对随机交错，共 240 次 run。
- 主分析：exact McNemar；非劣界 −5pp（sodus 不差于 direct 超过 5 个百分点）。
- 成本/时间：配对中位差 bootstrap 95% CI；非劣界 +20%。
- 先导 36 对只算热身，不用于结论。

## 控制

- 同模型同版本、固定预算上限；每个 run 隔离 HOME/临时目录；记录模型/路由/配置版本；禁止并发窗口重复计费（成本以 run metered + 面板余额双向核对，DB 为上界）。
- 任务顺序随机化；运行环境记录 Windows 版本、Python、sodus tag/HEAD。

## 停止规则

- 预算/relay 余额不足、或连续 5 次基础设施失败 → 暂停并记录，不拿小样本下结论。
- 数据不完整（verify 缺失、成本缺失）>10% → 分析只做敏感性报告。

## 产物

`reports/ab-2026-11-10/`：raw runs.jsonl、per-task 汇总、统计脚本、最终报告（含 CI、配对表、失败清单）。
