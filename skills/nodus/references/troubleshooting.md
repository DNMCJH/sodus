# 常见故障：按症状查

先跑一次自检，它把问题分成三类：

```powershell
python skills\nodus\scripts\nodus-check.py
# PASS  token   C:\Users\35336\.nodus\token (48 chars)
# FAIL  daemon  http://127.0.0.1:4199/health 连不上（[WinError 10061] …）
# SKIP  routes  daemon 没起来，先 nodus up
nodus-check: 1 项失败（daemon）—— 先修这个再派活
```

## daemon 不通

**症状**：`nodus-check.py` 的 daemon 行 FAIL；任何 CLI 报
`error: HTTP 400/500` 或直接连不上；看板打不开。

```powershell
nodus status                    # 能出数就说明 daemon 活着
Get-Content $env:USERPROFILE\.nodus\daemon.json   # {"pid": 276, "port": 4199, "started": …}
nodus down                      # PASS stopped pid=276
nodus up                        # PASS up pid=5252 http://127.0.0.1:4199/
```

`up` 会比对 state 目录指纹。如果它说端口已被另一个 `NODUS_HOME` 的 daemon 占着，
说明两个 state 目录在抢同一个端口——先把旧的那个 `down` 掉，或给新的 `--port`。

**401 Unauthorized** 是同一类问题的变体：daemon 读的 token 和你手上的不是同一个文件。
`nodus down` 再 `nodus up`，让它从当前 `NODUS_HOME` 重新读。

## 路由不全 ready

**症状**：`nodus-check.py` 的 routes 行 FAIL，或 `nodus agents` 有行不是 `ok`。

```powershell
nodus agents        # 每个 agent 的 vendor / protocol / ready，缺什么写在 missing
nodus routes list   # 6 条路由 + 回退链 + ready
nodus probe         # 只探活，不花生成 token
nodus doctor        # 看 secrets 那一行缺哪个 KEY
```

`routes` 不 ready 通常是两种：agent 的可执行文件不在 PATH（`claude` / `codex` /
`opencode`），或它依赖的 provider 没配密钥。`nodus doctor` 的 `secrets` 行会列出当前
能读到哪些 `*_KEY`。

## token 读不到

**症状**：token 行 FAIL。`%USERPROFILE%\.nodus\token` 不存在或内容不像 token。

跑任意一个 nodus 子命令都会生成它（48 字符 hex）。文件权限是 0600。别手抄这个值到
别的地方，也别把它塞进 MCP 配置——MCP 走 stdio，不需要 token。

## 命令报 `unknown task`

```
$ nodus run --route auto "写个 hello.txt"
error: HTTP 400: {"error": "unknown task: 写个 hello.txt"}
```

`nodus run` 的第一个参数是 **task id**。先 `nodus task "<目标>"` 拿 id：

```powershell
nodus task "写个 hello.txt" --accept "hello.txt 存在"
# task-5995110179e8
nodus run task-5995110179e8 --route auto --cwd C:\work\demo --wait 120
```

## `nodus proposals list` 报错

```
nodus: error: unrecognized arguments: list
```

`proposals` 没有 `list` 子命令，直接 `nodus proposals`。`--all` 才会带上已合并和已
拒绝的。

## 跑完了但没有验证

**症状**：`nodus runs` 里 `verify=none`，`nodus report` 说"没有任何验证记录"。

三个原因，按顺序查：

1. 用了 `--no-verify`。
2. worker 没改任何文件——自动验证只在文件有改动时触发。
3. 目录里检测不到测试命令。

```powershell
nodus verify --cwd C:\work\demo
# no test runner detected in C:\work\demo
nodus verify --cwd C:\work\demo --command "python -m pytest -q"
```

检测顺序：`pyproject.toml`/`pytest.ini`/`tox.ini`/`setup.cfg` → pytest；
`package.json` 的 `scripts.test` → npm/pnpm；`Cargo.toml`；`go.mod`。
要长期可验证，就在目标目录里放一个能被识别的测试入口。

## `verify=red`

测试跑过了，红的。证据在磁盘上，不靠记忆：

```powershell
nodus runs | Select-String verify=red
Get-Content $env:USERPROFILE\.nodus\runs\<run-id>\verify-red.txt -TotalCount 30
```

文件头有 `# ok=False exit=1 duration_ms=…` 和实际命令，后面是完整输出。

## 失败之后没人重试

daemon 每 300s 扫一遍失败。它停手的原因写在 `nodus show <run-id>` 的 `error` 和看板
的 `needs_you` 里。四类停手条件：

| 现象 | 含义 | 怎么办 |
|---|---|---|
| `重试不会好：…` / `deterministic` | 401/403/400、内容审核、模型不存在 | 改请求本身，重发只会计费 |
| `连续两次同样的错：…` | 同签名，再跑一遍一样死 | 看签名里的 cause 和失败阶段，改因 |
| `剩余额度不够再自动试一次：只剩 ¥…` | 日预算或任务预算不够下一次 | 加额度后手动重跑，或自己接手 |
| `这次重试预计 ¥…，超过自动上限 ¥0.50` | 单次超过 `NODUS_RETRY_COST_CAP` | 判断值不值再点重跑 |
| `自动重试已经到上限（3 次）` | 耗尽 | 人接手 |

相关环境变量：`NODUS_AUTO_RECOVER=0`（关自动恢复）、`NODUS_RETRY_LIMIT`、
`NODUS_RETRY_COST_CAP`、`NODUS_AUTO_RECOVER_MAX_AGE`、`NODUS_AUTO_RECOVER_INTERVAL`。

想让系统多花钱就调 `NODUS_DAILY_BUDGET`（或 `config.json` 的 `budget.daily_cny`）。
注意它到顶是**拒绝新 run**，从不杀正在跑的。

## 目录被锁

**症状**：

```
C:\work\demo is already being written by run-138753b90fa6 (agent=dsh, 42s);
wait for it, or use a different --cwd
```

```powershell
nodus locks                              # (no directory is locked) 或列出占用者
nodus run <task-id> --cwd X --wait 60    # 等它空出来，而不是立刻失败
nodus unlock X                           # 确认写者已经死了才用
```

锁是为了防两个 agent 改同一棵树：第二个的改动会盖在第一个没写完的状态上，两份 handoff
描述的都是不存在的树。`unlock` 是强拆，只在 `nodus reconcile` 之后确认没有活 run 时用。

## daemon 重启后有 run 卡在 running

```powershell
nodus reconcile
```

把"owner 进程已死但还标着 running"的 run 判失败。旧的失败超过 24h 就不会被自动重试，
只当历史记录。

## MCP 工具在客户端里不出现

- DSH：`cordis.patch.yml` 里必须是 `insert:` 数组，写成平铺条目会被静默跳过。改完要
  重启 DSH。
- Claude Code：`.mcp.json` 的 `command` 用绝对路径（GUI 的 PATH 和终端不一样）；
  没 `pip install -e .` 就带上 `PYTHONPATH`。
- 工具名形如 `mcp__nodus__nodus_task_create`。
- 客户端会清掉子进程的 `*_KEY`/`*_TOKEN`/`*_SECRET`，Nodus 不需要这些，但如果你的
  route 依赖 agent 自己读环境变量，就会在这里断。

## 输出是乱码

Windows 下 piped stdout 默认用 ANSI 代码页（gbk），中文会变成 `?`。

```powershell
$env:PYTHONIOENCODING = "utf-8"
```

`nodus-check.py` 自己会把 stdout 强制成 utf-8，不受这个影响。
