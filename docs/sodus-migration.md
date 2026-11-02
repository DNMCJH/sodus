> 产品名 Sodus，实现名 nodus。

# 从 nodus 名字切到 sodus 名字

这份是给「其它会话 / 其它机器上的 agent」看的操作说明：改名做了什么、怎么切、怎么退。

## 一句话

`nodus` 仍然可用，`sodus` 是新入口，两个命令跑的是同一份 CLI、同一个 daemon、同一个数据库。**不切也能一直用 `nodus`；切只用一条命令，而且默认不写盘。**

## 现在是什么状态（0.5.0）

| 东西 | 现在 | 说明 |
|---|---|---|
| 命令 | `nodus` 和 `sodus` 都能用 | `sodus` 转发到 `nodus.cli:main`，只改 usage 行里的名字 |
| Python 包 | `nodus`（`sodus/` 只是入口壳） | 没有模块搬迁，C2/C3 还没做 |
| 仓库目录 | `A:\VScode\Code\Projects\nodus` | 阶段 D 才改名 |
| 默认状态目录 | `~/.nodus` | 没有自动迁移，`~/.sodus` 不会自己出现 |
| daemon / 端口 / token | 不变 | 两个命令连的是同一个 daemon |
| MCP 工具名 | `nodus_task_create` … 仍是 `nodus_*` | 客户端配置不用改 |
| 托盘 / 桌宠配置 | 不变 | `pet_bridge`、`nodus-theme` 都是标识符 |

切换的顺序：**先什么都不做也能跑；想用新目录时再迁移。**

## 状态目录怎么选

`nodus/config.py` 按这个顺序解析（先命中先用）：

1. `SODUS_HOME`
2. `NODUS_HOME`
3. 已存在的 `~/.sodus`
4. `~/.nodus`

所以：

- 不设环境变量、也没有 `~/.sodus` 时，`sodus` 和 `nodus` 读的是同一份 `~/.nodus`——这就是为什么直接换命令不会丢数据。
- 只要 `~/.sodus` 存在，两个命令都会改读 `~/.sodus`；不用改 daemon 配置，但要把正在跑的 daemon 重启一次（它只在启动时解析 HOME）。

## 迁移命令

```powershell
nodus down                      # 建议：先停 daemon，避免复制时库里正在写
nodus migrate-home              # 默认 dry-run，只打印计划，不写盘
nodus migrate-home --apply      # 真的复制，并校验
```

`--apply` 做的事：

- 复制 `~/.nodus` 到 `~/.sodus`（sqlite 用 backup API 快照，`-wal`/`-shm` 折进快照，不单独复制）。
- 校验：文件数一致；每个库 `PRAGMA integrity_check` 为 `ok`；`task` / `run` / `artifact` 行数一致。
- 失败：删掉复制到一半的 `~/.sodus`，返回 1，原目录不动。
- 成功：打印 `done: ... verified`。原目录一个字节都不改。

参数：`--from DIR` / `--to DIR` 可以换源和目标（默认 `NODUS_HOME` 或 `~/.nodus` → `SODUS_HOME` 或 `~/.sodus`）。目标已存在且非空时命令直接拒绝，不覆盖。

**这个命令永远不会自动执行**，也不会移动或删除 `~/.nodus`。

## 切完怎么验证

```powershell
nodus --version                 # nodus 0.5.0
sodus --version                 # sodus 0.5.0
sodus stats                     # 和 nodus stats 同一份数据
nodus doctor                    # 目录 / token / 库 / 路由
```

如果两个 `stats` 数字不一样，先看 `sodus doctor` 打印的 HOME，再检查 `SODUS_HOME`/`NODUS_HOME` 是不是指向了别处。

## 回滚

迁移是复制，不是搬家，所以回滚就是删掉副本：

```powershell
nodus down
Remove-Item -Recurse -Force (Join-Path $env:USERPROFILE '.sodus')
nodus up        # 回到 ~/.nodus，一切照旧
```

删掉 `~/.sodus` 之后，解析顺序回落到 `~/.nodus`。唯一要注意的是：迁移之后在 `~/.sodus` 里新产生的数据也会一起删掉——回滚前先确认这份副本里没有你想留的东西。

如果只是想临时换个目录、不想迁移：设 `SODUS_HOME=<dir>` 即可，其它变量不用动。

## 其它会话要注意

- 脚本里写 `nodus ...` 的**不用改**。
- 新写的文档、提示词可以叫 Sodus，但命令示例仍写 `nodus`（`sodus` 是同义入口）。
- MCP 客户端配置（`.mcp.json` / DSH 的 mcpServers）保持原样：工具名没变，serverInfo.name 变化不影响调用。
- 不要在没人看的时候跑 `migrate-home --apply`：它会复制几百 MB 到 GB 级的目录，且必须有人确认结果。
