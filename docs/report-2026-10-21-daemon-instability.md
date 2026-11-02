> 产品名 Sodus，实现名 nodus。

# nodus 稳定性问题报告（2026-10-21）

**报告人**：调用方会话（大创调研，8 个并发 run）
**环境**：Windows，仓库 `A:\VScode\Code\Projects\nodus`，`A:\Anaconda\python.exe`
**用途**：用它并行编排多个 claude / codex / minimax agent 跑文献调研与脑暴

---

## 现象 1：daemon 存活约 49 分钟后退出，在跑的 run 全灭

- 03:25 以 `nodus.py serve` 起 daemon（当时作为后台任务）
- 04:14 进程退出，**exit code 1**，stderr 只有一行：
  ```
  nodusd listening on http://127.0.0.1:4199
  ```
  没有任何 traceback。
- 四个在跑的客户端同时报：
  `error: daemon unreachable: <urlopen error [WinError 10061] 由于目标计算机积极拒绝，无法连接。>`
- 后果：4 个 run 全部 exit 1，产物丢失。其中 codex 那路已经打印"六个候选已收敛"，说明**模型侧工作做完了，只是连接断开没落盘**——这是最疼的一点。

## 现象 2：改成独立进程后，客户端连接被重置

- 用 `Start-Process` 起 daemon（不挂在 job 下），端口 listening、`nodus doctor` 全部 PASS。
- 重新并发 4 路，其中 2 路客户端报：
  ```
  ConnectionResetError: [WinError 10054] 远程主机强迫关闭了一个现有的连接。
  ```
  栈顶在 `client.py` 的 `getresponse()` → `http/client.py` `_read_status()`。
- 此时 daemon 本身还活着（端口 listening，err 日志无新增内容，仍只有那行 listening）。

## 现象 2.5（重要）：exit 1 并不代表没有产物

- 第二轮里 2 个客户端报 `WinError 10054`、job 退出码 1，但**三个 run 的产物文件后来都出现了**（`r5_survey_theory.md` 26.9 KB、`r6_eval_rigor.md` 18.4 KB、`brainstorm_minimax.md` 43.7 KB）。
- 也就是说 agent 侧已经写完文件，坏的只是**客户端回报与 DB 状态**。
- 区别在于第一波：daemon 直接退出时，agent 进程跟着断了，那次是真的没产物。
- 结论：`exit 1` 只能说明"客户端没能完成交接"，不能推断"工作没做"。调用方需要主动检查产物目录，不能只看退出码。

## 现象 3：客户端退出后，DB 里留下 stale `running`

- `nodus runs` 显示 8 条 `running`（两波 × 4），而对应客户端进程早已退出。
- 没有自动 reconcile；要手动 `nodus reconcile --stale N`。
- 影响：拿 `runs` 判断"是否还在跑"会误判，也会让 `usage` / 报告统计失真。

## 现象 4：dsh adapter 启动即 401

- `--agent dsh` 一进 turn 就失败：
  ```
  401: {"message":"WorkBuddy: 401 Unauthorized","type":"authentication_error"}
  ```
  `turn_start` 之后 `inputTokens=0 outputTokens=0` 直接 error。
- 失败 run：`run-d0f489b4a7b9`（本次调用方发起）、`run-8bbe8ee83d88`（另一会话发起）。
- 猜测：dsh spec 没有 `env_secrets`，隔离 home `~/.nodus/agents/dsh` 里的凭据没接上。对比 minimax 有 `env_secrets={"ANTHROPIC_AUTH_TOKEN": "MINIMAX_API_KEY"}`。

## 现象 5：单次 run 无法指定模型

- `AgentSpec` 只有 `default_model`，且只有 minimax 用了它；claude 走的是快照 `~/.nodus/agents/claude/settings.json` 里的 `"model": "sonnet"`。
- 想按 run 换模型（opus-5-5 / gemini-3.8 flash）只能改共享快照，**并发时互相踩**。
- 日志里出现过：`[claude-code:unrecognized_model] {"model":"claude-opus-5-5[1m]","query_source":"sdk"}`。

---

## 期望行为

1. **daemon 崩溃必须留 traceback**：现在 err 日志只有一行 listening，无法定位。
2. **断连要标 failed**：客户端的 run 进程一退出，daemon 应把它标成 failed，而不是留 `running`；或提供自动 reconcile。
3. **`--model` 按 run 覆盖**：避免多个并发 run 抢同一个快照配置。
4. **dsh 凭据走 env 注入**：和 minimax 一致，不要把 home 复制当凭据来源。
5. 可选：daemon 重启后客户端能重连续跑（至少不要丢掉已完成的模型输出）。

## 复现

- 并发 4 个 `nodus run` 指向同一 task，cwd 为一个普通目录；
- 观察 30–60 分钟内是否出现 daemon 退出或客户端 `WinError 10054`；
- 事后 `nodus runs` 看是否残留 `running`。

当前库里就躺着 8 条 stale running，可直接用来验证 reconcile 行为。
