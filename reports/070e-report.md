# 0.7.1-b 修复报告

## 修复与测试

- B1：daemon.lock 使用内核文件锁，异常退出自动释放；禁用地址复用，Windows 设置 SO_EXCLUSIVEADDRUSE；先成功 bind 再初始化、恢复 journal、reap 和 reconcile。重复启动失败不会清理现有 run。tests/test_070e.py 验证同端口拒绝、锁互斥及 reap 未被调用。
- B2：daemon.auto_recover 默认 False；NODUS_AUTO_RECOVER 显式覆盖，隐式启动使用同一服务端入口。测试覆盖默认关闭、配置开启、环境覆盖；既有恢复行为测试显式开启。
- plan/review 超时调用 RUNNER.stop（Windows taskkill /T），并将 run 标为 failed、记录 deadline；测试验证 stop 与 run/plan 终态。
- Host：GET/POST 在鉴权与页面生成前检查唯一 Host，仅允许 127.0.0.1、localhost、[::1] 及端口；测试恶意 Host 403 和本机 Host 200。
- verify：控制台终止码（有符号/无符号）及启动/超时失败分类 infra，写 verify.infra 和 verify.retry 后仅重试一次；成功记 green，再次失败记 red。测试首次 infra 后通过和连续 infra。
- approve：重新计算规范化 plan JSON hash，重跑 validator，拒绝 HEAD/AGENTS 漂移及 warnings；存在 AGENTS 时不得省略 hash。tests/test_plan.py 验证 hash、HEAD、AGENTS 和 warning 拒绝。
- CLI reconcile 改 POST /reconcile，由 daemon RUNNER 执行；OpenAPI 增加端点，CLI 和 HTTP 均有测试。
- schema v7 正式迁移 plan_execution 与旧 warnings_json；移除运行时建表。旧库升级、重连幂等、旧数据保留均有测试。
- 更正 ab-power p10/p01 含义；HANDOFF 记录 git revert -m1 回滚与数据库备份恢复，以及 B3/B4 残余风险。

## 验证证据

- A:\Anaconda\python.exe -m pytest -q：退出码 0，100%；输出 reports/070e-pytest-final.txt。
- 定向测试 tests/test_070e.py、test_plan.py、test_server_policy.py、test_openapi.py：退出码 0；输出 reports/070e-targeted-final.txt。
- A:\Anaconda\python.exe -m sodus mcp install --check：退出码 0；Codex、Claude、DSH 均 configured。
- git diff --check：退出码 0。
- 重复 bind、Host 403、auto-recover 默认关闭均以隔离测试验证，未中断现用 daemon。

## 遗留

- 0.7.2：worker env 白名单、plan/review 只读沙箱未动；B3 仍阻塞无人值守。
- B4：共享 token/human 字符串可伪造，批准上下文复核不能替代身份隔离；后续独立批准权限。
- 预算配置恢复、DSH YAML 注释保留按 brief 留给后续。
- 未 git commit / push / merge。

## SUMMARY

SUMMARY：0.7.1-b 指定修复已完成并有测试；全量 pytest 退出码 0，定向测试和 MCP 三宿主检查通过。B3 与 B4 身份隔离残余风险留 0.7.2，未提交 git。
