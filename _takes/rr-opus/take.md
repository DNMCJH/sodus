# sodus 0.7.0 正式启用前复查（rr-opus，2026-11-09）

## 结论
**有人值守时可以作为日常主版本，但要先修 B1/B2/B5（改动小，当天能完成）。在 B3/B4 修好之前，不能无人值守，也不能批准执行 plan。** 现状比担心的好的地方：只绑 loopback，非 loopback 默认拒绝；token 用 hmac 比较；secrets.json 按 agent 最小注入；MCP 不暴露 approve；release_check 加约 1091 项测试；无窗口回归测试。

## 阻塞项（按严重度）
| # | 严重度 | 位置 | 问题 | 最小修法 |
|---|---|---|---|---|
| B1 | 高 | `server.py:serve()`、`_QuietServer` | 沿用 ThreadingHTTPServer 默认的 allow_reuse_address，Windows 上可以双绑 4199；而且 `reap_orphans()`/`plan.reconcile()` 在 bind **之前**执行，第二个实例一启动就会把第一个实例的活 run 判成孤儿。这正是 03:27 事故的成因 | `allow_reuse_address=False`，并加 `SO_EXCLUSIVEADDRUSE`；在 HOME 下加文件锁；先 bind 成功再 reap |
| B2 | 高 | `recover.enabled()` | 默认值是 `"1"`；`client.ensure_daemon` 隐式拉起 daemon 时不固定这个值，所以“关闭”只对手工带 env 启动的那个进程有效 | 默认改为关，写进 config.json |
| B3 | 高 | `agents.py` codex/claude 命令；`plan._start_planner_run` | 所有 worker 都以 bypass sandbox 运行；plan/plan_review 直接在目标仓库 cwd 里跑，“只读”只靠提示词；env 用的是 `os.environ.copy()`，会把 daemon 的全部环境变量传下去 | plan 类 run 加 `--sandbox read-only` 或放到临时 worktree；结束后 `git status --porcelain` 不为空就判失败；env 改为白名单 |
| B4 | 中高 | `plan.approve`；`server.py` 中 /plan 的 depth 检查 | 批准人身份只是一个字符串；`X-Sodus-Depth` 由客户端自报，而 `NODUS_ROLE` 检查的是 daemon 自己的环境，形同虚设。worker 读得到 `~/.nodus/token`，用 curl 就能批准 | 0.7.1 加执行能力之前，approve 只走 CLI 的 TTY 交互确认，或改用 worker 读不到的单独凭据 |
| B5 | 中 | `~/.nodus/config.json` | 为跑 planner 临时把预算调成 daily/soft=200 后没有恢复，软闸门实际失效（relay 只剩约 123） | 从 `.bak-20261109*` 恢复 50/20 |
| B6 | 中 | `plan._wait_run`/`review` | 超时后只抛异常，不停 run，钱继续花；HTTP handler 同步阻塞 | 超时时 `RUNNER.stop`；review 改成后台执行 |
| B7 | 中 | `cli/commands/mcp.py:142` | drift 时整段覆盖，用户自定义的 env 会丢；写文件不是原子的 | 合并保留 env；写临时文件后 `os.replace` |
| B8 | 中低 | `plan._connect_meta` | `plan_execution` 表在运行时 CREATE，绕过了 `db._MIGRATIONS`，schema 有两个来源 | 改成 v7 迁移 |
| B9 | 中低 | `cli/commands/system.py:reconcile` | CLI 直接调本地 RUNNER，形成第二个所有者，可能把长时间没输出的活 run 判失败、释放写锁 | 改走 daemon HTTP |

## Q1 整体性
分层方向是对的：daemon 是唯一 run 所有者，MCP/CLI 都走 HTTP。还在打补丁的地方：①一个 plan 要用 plan、plan_execution、task、run 四处记录；②ask 和 plan 都是“异步 LLM run 加轮询”，却是两套状态机；③board、/state、/pet、statuslight 是四个读模型；④daemon 有两条启动路径（`daemon.start`、`client.ensure_daemon`）；⑤B9 的 CLI 直连。
收口办法（不加新功能）：plan 和 ask 都并成 `run.kind`，共用一套状态机，plan 表只存契约；`/state` 作为唯一读模型，board 和 pet 只负责渲染它；启动路径合成一个函数，并且只有它能写 daemon.json。

## Q2 安全（威胁模型）
- 凭据：secrets 已最小注入，但 env 全量继承（B3）；token 文件权限 644，worker 读得到（B4）。
- 写路径和 git：plan/review 没有沙箱（B3）。HANDOFF 里的回滚写的是 `git reset --hard`，应改为 `git revert -m1 94555b8`。
- 配置写入：B7。
- worker 输出：`_extract_json` 只校验结构，是合理的；但不能让它驱动执行。
- 递归：MCP 和 ask 有防护，HTTP 头可以伪造（B4）。
- 暴露面：loopback 加 token，合格；`/pet` 和 `/openapi.yaml` 公开，风险低。
- 无人值守误操作：B2、B5。

## Q3 鲁棒性
必修：B1、B2、B6、B9。SQLite 方面：WAL、timeout=10、连接锁都有，合格；B8 要补迁移。verify 的 3221225786（STATUS_CONTROL_C_EXIT）根因还不清楚，我没有复现。在查清之前，这个退出码应归为“基础设施失败”并自动重跑一次，不应判任务失败。daemon 重启后 planning 只会被置为 failed，不会恢复，可以接受，但看板上要能看到。

## Q4 高效性
指标：隐藏测试通过率（主指标）、墙钟时间、总成本（含 planning/review/retry/verify）、人工介入次数、返工次数、首次可用时间。
现状：开销结构是在 worker 之外再叠 opus planning（约 ¥0.86 量级）、sol review 和 retry；`bench.py` 只有经 sodus 的一组，**没有直接调 agent 的对照组**，所以目前无法得出“更省、更快或更好”的结论。
预注册设计：选 12 道中等难度题（eval/tasks 现有题会满分，有天花板，需要换题），两组同模型、同预算；每题每组 10 次，配对随机交错，共 240 次 run；每次 run 都用临时目录和干净的 HOME。主分析用 exact McNemar，非劣界 −5pp；成本和时间做配对中位差的 bootstrap，界为 +20%。备注：`2026-11-08_ab-power.md` 里 p10/p01 的含义写反了，需要更正。

## Q5 其它
回滚：`~/.nodus/nodus.sqlite3` 没有自动备份，需要补。daemon 用 `cwd=REPO_ROOT` 从仓库源码加载，主仓库一切分支，下次重启的代码就跟着变了。多宿主漂移：DSH 需要重启才会生效。可观测性：daemon.log 只追加、不轮转。供应商：plan 默认走 opus、review 写死 sol，应改成可配置。

## Q6 正式启用 checklist
1. `netstat -ano | findstr :4199` 只有一个 LISTENING，且 pid 等于 daemon.json 里的 pid；再手动 `sodus serve --port 4199` 一次，必须以非 0 退出，`sodus runs` 里的状态不变。
2. kill daemon 后执行任意 `sodus status` 触发隐式拉起，daemon.log 中应出现 “auto-recover disabled”。
3. 用 utf-8 读 config.json，budget 应为 daily 50、soft 20。
4. 在一个临时仓库里跑 `sodus plan new`，提示词里诱导它写文件；结束后 `git status --porcelain` 为空，且 run 被标记失败。
5. 设 `NODUS_ROLE=worker` 后执行 `curl -X POST .../plans/<id>/approve`，返回 403。
6. 把 plan timeout 设为 5s 跑 review，之后 run 状态是 stopped，不是 running。
7. 先在 codex 的 sodus 条目里加自定义 env，再跑 `sodus mcp install`，env 应该还在；配置文件写入要用 `os.replace`。
8. `pytest -q` 和 `scripts/release_check.ps1` 都以 0 退出。
9. 连续跑 20 次 verify，3221225786 出现 0 次，或者被归为 infra 并重跑。
10. 在临时 clone 里演练一次 `git revert -m1 94555b8` 加数据库备份恢复。
11. A/B 预注册文档先提交，再开始跑。

## 风险
不修 B1–B3 就无人值守的话，最可能出现的是孤儿 run 被复活后改坏仓库、预算失控。

## SUMMARY
0.7.0 在有人值守时，修好 B1（双实例/先 reap 后 bind）、B2（auto-recover 默认开）、B5（预算没恢复）后可以作为日常主版本。无人值守和 plan 批准执行要等 B3（worker 无沙箱、plan 只读只靠提示）和 B4（批准身份可伪造）修好。目前还没有效率结论，需要按预注册的直接 agent 对 sodus 配对 A/B（240 次 run）来验证。
