# 0.8.0-b1 B3 安全控制验收

## 实现

- `agents.build_env` 从固定基础键白名单构造环境，仅额外注入当前 provider、agent 声明的 secrets、配置目录与 worker 标记，不再复制 daemon 全部环境。
- `Runner.start(readonly=False)` 经执行线程和 attempt 透传到 `agents.argv`；plan/review 始终传 True。Codex 使用 `--sandbox read-only`，移除 bypass；Claude 使用 `--permission-mode plan`；DSH/opencode 记录 `run.warning`。
- planner 在 Git 仓库使用 detached HEAD 临时 worktree，保留仓库内子目录定位；非 Git 使用过滤 copytree，排除依赖、缓存、checkpoints、符号链接。启动失败立即清理，plan/review 收集器在 finally 清理（含超时/结果校验失败）。隔离失败明确 warning 后回退原 cwd。
- 原 cwd 仅保留在计划上下文用于 validator/base_sha；handoff 与批准流程未修改，没有 git commit/push/merge。
- 更新 `docs/architecture.md` 安全段和 CHANGELOG（未增加版本号）。

## 验证证据

- 目标测试：`A:\Anaconda\python.exe -m pytest -q tests/test_agents.py tests/test_plan.py tests/test_runner_policy.py`，退出码 0。
- runner/memory 回归：`A:\Anaconda\python.exe -m pytest -q tests/test_runner_policy.py tests/test_runner_memory.py tests/test_memory.py`，退出码 0。
- env 测试覆盖 SECRET_LEAK、继承的 daemon API key 不泄漏、所选 provider secret、worker role、全部 agent 基础 PATH。
- argv 测试对照普通/readonly 模式；runner 测试验证 start 的 readonly 到达 attempt。
- fake planner 在真实临时 Git 仓库执行：断言 cwd 不同、readonly=True；向临时 worktree 写文件，原树文件不存在且前后 `git status --porcelain` 字节一致；成功和启动失败后目录删除且 worktree 注销。
- 非 Git 测试验证过滤副本、源文件不变及清理；plan/review 收集失败测试验证 finally 清理；隔离创建失败测试验证原 cwd 回退及 warning。
- 初次回归发现旧 attempt 替身签名需增加 readonly，已修正；编辑时启用 PYTHONUTF8 导致 Windows tasklist 中文输出解码失败，最终全量验收移除该环境设置，未为此修改产品代码。

## 残余风险

- 无 OS 级隔离，同用户仍可通过绝对路径读取 token/凭据文件或访问主树；DSH/opencode 无原生只读开关，临时 cwd 不能阻止恶意越界。Git worktree 元数据仍关联原仓库。
- Git 隔离是 HEAD 快照，不包含未提交修改。非 Git 副本不包含被过滤内容。
- 隔离失败按简报回退原 cwd 并记录 warning，此时不能保证只读。进程崩溃/断电可能留下临时目录；正常路径清理失败会记录异常。
- 批准人身份独立校验及后续执行流程属于 B4/后续范围。

## SUMMARY

B3 实现、目标测试与文档已完成；全量 pytest 最终结果见下方验收记录。

最终验收：`A:\Anaconda\python.exe -m pytest -q` 全量执行至 100%，退出码 **0**；完整输出见 `reports/080b1-pytest-final.log`。最终运行未设置 PYTHONUTF8，使用系统默认编码。`git diff --check` 退出码 0。

SUMMARY: 0.8.0-b1 验收通过；env 白名单、readonly argv、隔离 cwd/清理均有测试，全量 pytest 退出码 0；未修改 handoff/批准流程，未提交 Git。下一位可接 B4 独立批准身份，或审阅本次未提交差异。
