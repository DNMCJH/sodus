# OPS：sodus 0.7（goal 模式，2026-11-09）

- 03:xx 合并 0.6.0 到 main（a6acd0c，tag v0.6.0），daemon 重启为 0.6.0（pid 126640）
- 03:xx 从 main 开 worktree `A:\VScode\Code\Projects\nodus-0.7`（feat/sodus-0.7）
- 预算：config.json budget.daily_cny 50→200（backup config.json.bak-20261109），relay 余额 80.62 为真实硬约束

## 计划
- 0.7.0-a：MCP 全转 HTTP + `sodus mcp install`（codex/claude/dsh）
- 0.7.0-b：/state 轻量端点 + 展示层移出 server.py
- 0.7.0-c：planner 契约 + validator + POST /plan + sodus plan + kind='plan' 只读 run
- 0.7.1+：plan 物化、里程碑评审/replan、并行与 A/B

## 监控规则
- 每轮开始检查运行中 run；15–20 分钟一次；结束回合前检查并写 ops_monitor.log。

## 阶段记录（2026-11-09 03:22:46）
- 0.6.0 已并入 main：merge commit a6acd0c，tag v0.6.0；daemon 重启为 0.6.0（pid 126640，/health ok）
- main A:\Anaconda\python.exe -m pytest -q：EXIT=0
- 0.7 worktree 已建（feat/sodus-0.7 @ a6acd0c）
- 当前 run：run-f2b8cb081fa3（sol，0.7.0-a MCP 收口+install）；检查中


## 事故：daemon 126640 死亡（2026-11-09 03:28:25）
- 03:18:49 启动 daemon 126640（0.6.0，NODUS_AUTO_RECOVER=0），03:23 后某刻死亡，日志无 crash 记录
- 0.7a run-f2b8cb081fa3 被标 orphaned（13 files kept）
- 03:27:05/06 两次 ensure_daemon 抢跑：pid 149716（在听）和 122100（写进 daemon.json，未监听）
- 新 daemon 默认开 auto-recover，retry 了 0.7a（run-943c6e95e9d8，1/3）和旧 W3a（run-343e652652e3，2/3）；后者已 HTTP stop
- 教训：daemon 重启后必须确认 daemon.json pid == 监听 pid，否则 down/up 打错进程；auto-recover 默认值在 ensure_daemon 拉起时会复活


## 阶段记录（2026-11-09 03:41:30）
- 0.7.0-a 完成并提交：abf04a7（mcp 全 HTTP 化，无 runner/RUNNER 引用；installer 支持 codex/claude/dsh；全量 pytest EXIT=0）
- daemon 双实例事故根因：03:27 两个 nodusd 同时监听 4199（SO_REUSEADDR），一个改为孤儿、auto-recover 复活旧 task；已 kill 两个并重起单实例 pid 155212（auto-recover off），daemon.json 一致
- sodus mcp install --check：codex/claude/dsh 全部 configured，exit=0
- DSH dsh --profile web --dump-config 校验通过；本回合 DSH 会话已出现 mcp__sodus__* 工具（入口生效）

## 下一回合（0.7.0-b）
- /state 轻量端点 + 展示层外移


## 当前回合（0.7.0-b，2026-11-09 03:41:53）
- 任务 task-c8e143ebae5f：/state + board 展示层外移（sol）
- 预计 20–35 分钟；下一轮开始检查


## 阶段记录（2026-11-09 03:54:39）
- 0.7.0-b 完成并提交：92ed615（/state p95 26-31ms；server.py 2767→725 行；board 键不变；全量 pytest EXIT=0）
- 0.7.0-c1 任务：task-e6855bb9434b（plan 契约+schema v6+validator+CLI，sol）


## 阶段记录（2026-11-09 04:11:19）
- 0.7.0-c1 完成并提交：5821e52（plan.json v1 + schema v6 + validator + CLI；1065 tests exit=0）
- 0.7.0-c2a 任务：task-4f66849549a0（planner 执行链 + review/approve + HTTP，fake 测试；sol）


## 阶段记录（2026-11-09 04:26:46）
- 0.7.0-c2a 提交：ae7c9b4（planner run + fake review/approve + HTTP；全量 1065+ 测试 exit=0）；sodus 自身 verify 被信号打断（3221225786），Lead 独立复跑 exit=0
- 发现所有权缺口：CLI plan new/review/approve 直接调本地 plan 服务（runner 第二所有者）；已派 task-c47299a35279 修（c2b）


## 阶段记录（2026-11-09 04:44:06）
- release_check（0.7.0 树）：PASS exit=0（修了 schema 硬编码 5 的旧断言，a3e1df7）
- opus review（run-34062f672382，¥0.86）：需修后合并；阻塞 1 installer 解释器写死、2 plan 契约依赖未打包 docs、3 planning 永不终止/重启卡死；建议同版修 4 递归防护；5–7 留 0.7.1
- 已派 task-c3（sol）修 1–4


## 0.7.0 发布记录（2026-11-09 05:09:15）
- 0.7 review 阻塞 1-4 已修：dcc80ba（sys.executable、契约包内化、planning 超时/reconcile、递归防护）；no-window 修复 dd7ae67 + 测试
- release_check PASS（EXIT=0）
- merge 到 main：94555b8，tag v0.7.0；daemon 重启 0.7.0 pid 150512，单实例；/state 实测 162ms 首次、版本 0.7.0；mcp install --check 三宿主 configured
- 已建 0.7.1 worktree（feat/sodus-0.7.1 @94555b8）；正在用 opus 生成 M4–M6 plan（kind='plan'，daemon 所有）


## 完成（2026-11-09 05:13:10）
- 0.7.0 已发布：main merge 94555b8 + docs 8cdd34c，tag v0.7.0；daemon 0.7.0 pid 150512 单实例
- release_check PASS；/state 实机 162ms；mcp install --check 三宿主 configured；no-window 修复 dd7ae67 已并入
- M4–M6 真实 opus plan：plan-70cceecf7ca5，validated，base_sha 匹配，errors=[]，5 节点；sol review verdict=revise（3 findings），批准待修订
- 预算：daily/soft 临时 200（备份 .bak-20261109 / -soft）；relay 剩 123.68

