# 0.7.0-c2a 执行报告

## 实现

- config.routing.planner / planner_route() 默认 opus，CLI --route 覆盖。
- plan.create 建 kind=plan run，目标 cwd，关闭 verify/handoff/memory，附 plan-only note。
  Prompt 要求只读侦察 git HEAD、tracked files、AGENTS.md、测试命令和真实相对路径，
  内嵌 v1 契约，最后 assistant message 仅一个 JSON 对象。
- 从全部 event 中选最后 assistant message（不受分页上限影响），去可选 json 围栏并 json.loads，
  再走 c1 validator。成功原子保存 canonical plan_json、SHA256、base_sha、nodes；
  run/解析/校验失败保存 failed/errors_json，保留 run_id；新 create 即独立重试。
- plan_execution 是兼容现有 v6 的 additive 元数据表，保存 cwd/run/review；
  不改 task 状态、不派发节点，不变更既有 schema_version。导入的新 plan 也记录 cwd。
- review 使用独立 kind=plan_review / sol run，携带 plan JSON 和仓库 cwd；
  验证 verdict=approve|revise|reject 和 findings 数组后存 reviewed/review_json。
  重评前清除旧 verdict；失败不会保留旧批准资格，review_run_id 保留运行证据。
- approve 仅接受显式 by=human + reviewed + verdict=approve，记录 approved_by/at。
- CLI new/review/approve 和 show/list 元数据；--no-wait 交给 daemon 持续收集。
- 鉴权 HTTP POST /plan (202)、GET /plans/{id}、POST review/approve；
  create 在启动 run 后用后台线程等待/抽取，review 同步。OpenAPI 和命令/生命周期文档已更新。

## 测试证据

定向 pytest tests/test_plan.py tests/test_openapi.py -q 已退出 0（40 项，随后追加 runner fake 测试）。
Fake 覆盖正常 JSON/围栏、垃圾/缺字段 failed、重新创建重试、review 三种 verdict、
无 review/非 human/revise/reject 门禁、无效重评清除旧 verdict、CLI wait/no-wait、
真实 HTTP server 的鉴权/202/轮询/404/review/approve，以及 runner start 参数和失败结果。
所有新增模型执行入口均 monkeypatch fake；无真实模型调用。
全量结果见下文最终验证与 reports/070c2a-pytest.log。

## 边界与遗留

只读由 prompt、run note 和 verify_run=False 提示，未提供强制文件系统沙箱。
by=human 记录调用者显式声明，不独立证明调用者为人类。
失败通过新建 plan 重试，不覆盖原尝试；旧 c1 导入记录未存 cwd，需新建/重新导入后 review。
后台 worker 不提供 daemon 重启后的自动恢复；c2a 不包含节点执行、预算派发和审批后的执行。
未 git commit/push/merge，保留既有未跟踪文件。

## SUMMARY

0.7.0-c2a planner execution, fake review/approval gates, CLI, authenticated HTTP endpoints, OpenAPI and lifecycle documentation are implemented. Full `A:\Anaconda\python.exe -m pytest -q` completed with exit code 0 and 100% progress; no real model call was made. No git commit was created; the next handoff is to review the diff and, if desired, add daemon restart recovery or a filesystem sandbox in a later change.
