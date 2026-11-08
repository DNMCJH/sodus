# 0.7.0-c3 review fixes

## 范围
仅修 brief 指定的 1–4；无 git commit / push / merge，未调用真实模型。

## 修复及回归覆盖
1. installer 的 command 使用 sys.executable；test_interpreter_and_legacy_drift 验证实际安装文件与当前解释器一致。旧绝对路径若与当前 sys.executable 不同，--check 报 drift 并返回 1；相同则 configured。未改 medium 7 的合并/写入策略。
2. PLAN_CONTRACT 内置于 nodus.plan，不再读取仓库 docs。契约装配在 create 的 try 内；异常持久化 failed/errors_json。test_contract_packaged_and_read_failure 同时覆盖 Path.read_text 不可用时成功生成、契约加载注入 OSError 时 failed 且有错误。
3. config.plan_timeout 默认 1800 秒，config.json plan_timeout 配置，NODUS_PLAN_TIMEOUT 优先；非正数/非有限值/无效值回退默认。_wait_run 用 monotonic 总 deadline，分段有界 wait；异常由 _finish_create 持久化 failed。serve 在 reap_orphans 后、监听前调用 plan.reconcile，统一将旧 planning 置 failed 并留 errors（跨 workspace，幂等）：即使 run 已完成或仍在运行，旧收集线程也已丢失，不尝试恢复。CLI new --wait deadline 为 plan_timeout+30 秒，GET 超时不超过剩余预算、禁内部重试，超时输出 failed 并返回 1。测试覆盖永远 running、真实 SQLite run failed/completed/running/missing、重复 reconcile、CLI deadline/网络超时和配置。
4. POST /plan、/plans/{id}/review、/plans/{id}/approve 对 depth>=1 或 daemon 环境 NODUS_ROLE=worker 返回 403 recursion_denied；非法 depth 返回 400。参数化测试覆盖三个接口 × depth/worker 两种来源，断言不派发。正常 HTTP 测试隔离执行环境继承的 worker 标记。

## 验证
- 定向命令：A:\Anaconda\python.exe -m pytest tests/test_plan.py tests/test_mcp_install.py -q，58 项通过、退出码 0；见 070c3-targeted.log。
- A:\Anaconda\python.exe -m sodus mcp install --check：退出码 0，codex/claude/dsh 均 configured；见 070c3-install-check.log。只读检查，未写宿主配置。
- 全量首次运行退出码 0；代码/测试定稿后的全量复核亦退出码 0（1091 项通过，无失败）；见 070c3-pytest.log 与 070c3-pytest-exit.txt。
- scripts/release_check.ps1 保持原样，可后续复跑；本轮未执行发行构建与隔离安装验证。

## 遗留
review medium 5–7 保留至 0.7.1：review 仍同步占 handler/固定 sol；validator 漂移与语义增强未做；installer env 保留、DSH skip、并发/原子写入未做。共享 _wait_run 因阻塞 3 获得等待期限，但未改 medium 5 的接口形态。低优先级 8–10 未处理。
超时终止的是 plan 等待并将 plan 记 failed，不主动杀掉底层 worker；只读仍是提示约束。CLI 的本地 failed 输出不冒充服务端数据库更新；服务端由自己的 deadline/reconcile 收口。

## SUMMARY
完成 0.7.0-c3 四项阻塞修复，每项均有回归测试；定向 58 项通过，全量 1091 项通过，pytest 退出码 0；mcp install --check 退出码 0。未提交 git，medium 5–7 留至 0.7.1。下一位审阅 diff 与本报告，并复跑 scripts/release_check.ps1；本轮未执行 release_check。
