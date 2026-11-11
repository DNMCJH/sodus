# sodus 0.7.0 正式启用前复查 brief（2026-11-09）

## 背景事实（以仓库与运行状态为准）
- 0.7.0 已在 main：merge 94555b8、tag v0.7.0；daemon 0.7.0（单实例 4199，auto-recover 关闭）；release_check PASS。
- 0.7 交付：MCP 全转 daemon HTTP + `sodus mcp install`（本机 codex/claude/dsh 已 configured）；只读 `GET /state`；看板展示层移入 `nodus/board.py`；planner 契约（plan.json v1、schema v6、validator、`sodus plan new/review/approve`、`POST /plan`、`kind='plan'` 只读 run）。
- 已知事故：曾出现两个 nodusd 同时监听 4199（一个变孤儿、auto-recover 复活旧 run）；sodus 自带 verify 多次以 3221225786（CTRL_C）终止；codex worker 偶发 exit=1；plan 曾因预算软闸门/未打包 docs 卡 draft（已修）。
- 已知缺口：review 同步占线程无超时；validator 不比对 base_sha 漂移/AGENTS.md 哈希；installer drift 覆盖会丢用户 env、非原子写；plan 节点执行/物化/replan 未实现；批准人身份仅 `--by human` 声明；只读无沙箱；daemon 重启不恢复 planning。
- 真实 M4–M6 plan 已生成且 validator 零错误，但 sol review verdict=revise（3 条 findings），尚未批准执行。

## 用户要求复查的维度
1. **整体性**：系统是否一体、协调、自然，而不是补丁式堆功能（用户多次强调）。
2. **安全性**：避免泄露（凭据/记忆/项目数据）、破坏（写文件/删数据/错误 git 操作）、意外操作（无人值守下误执行）。
3. **鲁棒性**：稳定、好用、不易崩；故障可恢复。
4. **高效性**：sodus 本应提升效率，不能让同样难度的任务经 sodus 后更贵、更慢、质量更差；任务完成指标多样，需要综合评估。
5. **其它**：维护性、可观测性、发布/回滚、文档、成本治理、多宿主漂移、DSH 插件/面板、供应商锁定、数据迁移、隐私等。

## 问题
Q1 整体性：按 ADR/设计核对 0.7 的分层（core/HTTP 契约/adapters）。指出仍在打补丁的地方、重复状态源、职责错位、概念不一致（例如 plan 与 task/run、ask 与 plan、board 与 state 与 pet、MCP 与 CLI 的多入口），给出收口方案（不是再加功能）。
Q2 安全：给威胁模型（凭据、写路径、git 操作、配置写入、prompt/worker 输出信任、路径穿越、递归、无人值守误操作、daemon 暴露面）。逐条给现有控制、缺口、严重度、最小修法。
Q3 鲁棒性：daemon 生命周期（单实例、SO_REUSEADDR、重启/崩溃、孤儿、auto-recover、watchdog）、SQLite 锁与迁移、verify/CLI 被信号终止、worker 退出码异常、超时/重试、installer 原子性；给出日常使用前必须修的项与验证方法。
Q4 高效性：先定义多指标（隐藏测试/verify 通过率、wall time、总成本含 planning/review/retry、人工介入次数、返工、易用性）；评估 0.7 当前开销结构；设计一个可预注册的对比（直接 agent vs sodus 派活；任务集、样本量/功效、随机化、隔离、记账口径）；说明现在能得出/不能得出什么结论（禁止拿 n=1/n=3 下定论）。
Q5 其它：按上述清单逐项；指出用户没提到但会影响正式启用的点。
Q6 结论：给出「当前能否作为日常主版本」的明确判断；阻塞项按严重度排序；一个可执行的正式启用 checklist（每项有可验证命令/证据）。

## 输出
当前目录 `take.md`，中文 ≤1500 字；结构：结论 → Q1–Q6 → 风险 → checklist；末尾 SUMMARY。只读仓库，不改代码。
如果某个维度现状比用户担心的好，也直接说，不要为了平衡硬凑问题。

