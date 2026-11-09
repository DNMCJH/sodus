# 0.8.0-b1 brief：B3 worker 环境隔离 + plan/review 只读

## 边界
- 工作区 `A:\VScode\Code\Projects\nodus-0.8b`（feat/sodus-0.8-security，基于 main c27dc25）。**不 git commit / push / merge**。
- 只做 B3：worker 环境白名单、plan/review 只读执行；不改 handoff、不改批准流程（B4 另开）。
- 先读 `nodus/agents.py`（build_env/argv/AgentSpec）、`nodus/runner.py`（start/run）、`nodus/plan.py`（_start_planner_run/create/review）、`nodus/profiles.py`、`tests/test_agents*`/`test_runner*`/`test_plan*`。

## 需求
1. **env 白名单**：`build_env` 不再 `os.environ.copy()`；从固定基础键构造（PATH、PATHEXT、SystemRoot、WINDIR、COMSPEC、TEMP、TMP、USERPROFILE、HOMEDRIVE、HOMEPATH、HOME、APPDATA、LOCALAPPDATA、ProgramData、ProgramFiles、ProgramFiles(x86)、NUMBER_OF_PROCESSORS、PROCESSOR_ARCHITECTURE、OS、LANG、LC_ALL、TZ、HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY 及小写）再注入 provider secrets、CLAUDE_CONFIG_DIR/CODEX_HOME、NODUS_RUN/ROLE/DEPTH、NODUS_DSH_BIN/NODUS_NODE（如需要）；不得继承任意 daemon 环境变量。
   - 测试：设置 `SECRET_LEAK=...` 后 build_env 不含；provider secret 存在；`NODUS_ROLE=worker`；四种 agent 均有基础 PATH。
2. **只读参数**：`runner.start(..., readonly: bool=False)` 透传到 `agents.argv`；plan/plan_review 调用设 True。
   - codex：readonly 时用 `--sandbox read-only`，去掉 `--dangerously-bypass-approvals-and-sandbox`；
   - claude：readonly 时用 `--permission-mode plan`，去掉 `--permission-mode bypassPermissions`；
   - dsh/opencode 无只读开关：在隔离 cwd 中运行并记录 warning（不阻止，但不碰主树）；
   - 测试 argv 包含/不包含对应 flag。
3. **隔离 cwd**：`plan._start_planner_run` 在 git 仓库创建临时 `git worktree --detach`，非 git 用过滤 copytree（排除 .git/.venv/node_modules/cache/checkpoints 等）；worker cwd 用临时目录；原 cwd 只留在 plan 行用于 validator/base_sha；run 结束/失败后清理临时 worktree/目录（finally）；测试用 fake runner 验证传入 cwd≠原目录、原目录文件未被改、清理发生。
   - 如果隔离创建失败，回退为原 cwd 并记录 warning（不静默假装只读）；测试覆盖。
4. **文档**：`docs/architecture.md` 安全段补 B3 控制与残余风险（同用户可读 token 文件、无 OS 级隔离）；`CHANGELOG.md` 不加版本号；`reports/080b1-report.md`。

## 验收
- 目标测试 + 全量 `A:\Anaconda\python.exe -m pytest -q` 退出码 0；
- 手动：`sodus plan new` 后 worker cwd 在临时 worktree、原仓库 `git status --porcelain` 不变（可用 fake 或真实短任务，不花大钱）；
- 080b1 报告含证据、残余风险、SUMMARY。
