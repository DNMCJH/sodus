# Opus review：feat/sodus-0.6 vs v0.5.1

**结论：需修后合并。** 先修 H1–H2、M1–M3；其余可以记入 0.6.1。

## 高
- **H1 `dream.count`/`extract`**：`output_json` 直接存 final.md 原文，`count` 直接 `json.loads`，并读取 `item['text']`。模型输出带 ```json 围栏或缺字段时就会抛异常，批次一直停在 `extracted`。之后 `plan` 每次返回同一批次，`extract` 命中缓存，`count` 再抛同样的异常，该 workspace 的 dream 会永久卡住。
  修法：`extract` 里去掉围栏并校验 schema；解析失败时把批次写成 `failed`+error，允许重新 plan。测试：输出带围栏、垃圾文本、缺 `text`，确认批次失败后能恢复。
- **H2 `memory.recover_journals` 没有任何测试**（tests/ 中找不到调用）。W3b 声称 recovery 幂等，但没有证据。
  测试：写完 journal 后崩溃（文件 hash 等于 before）、replace 后 DB 提交前崩溃（等于 after）、文件被第三方改动（failed，journal 保留），每种情况各跑两次。

## 中
- **M1 `ask.view`**：daemon 重启后执行线程已不在，run 被回收为 `failed`，但 ask 行还是 `running`。view 只在 run 为 queued/running/stalled 时才覆盖状态，所以这个 ask 会一直显示 running，直到 24h 过期。修法：run 终态也映射到 ask，或在 serve 启动时对账 ask。
- **M2 `server._memory_board`**：只要有一条 staged 候选就 `return staged`，0.5.1 的 harvest 候选会整体从看板消失，属于回归。修法：合并两类候选并去重。
- **M3 `GET /memory/candidates`**：默认 workspace 取 `saved_project()`，但 dream 候选存在 `config.workspace_id()` 下，默认查询看不到。`evidence_count` 用 `len(sources_json)` 计算，dream 候选的 sources_json 是 dict，结果恒为 0，与看板的 `candidate_evidence` 口径不一致。
- **M4 `memory.apply_ops` 漂移检测**：手改 facts 区块后，旧的 `_append`（adopt/harvest）直接抛 RuntimeError，而文档写明 AGENTS.md 由人手改，这是 0.5.1 回归。另外 `rollback` 只重放 ops，reindex 吸收进来的手改内容会被回滚静默丢弃。
- **M5 `dream.apply_approved` 只由 `daily_once` 调用**：候选通过 API 标成 `adopted` 后，最晚要隔天才落盘，当天已跑过时还要再等一天。这期间“adopted”并不在事实源里，违背“状态派生、AGENTS.md 是事实源”。修法：审核时触发 apply，或引入 `approved` 中间态。
- **M6 `episodes.record_episode`**：verdict 在 run 结束时固化，主键去重后，事后 verify 转绿的 run 永远是 unverified，进不了 dream 证据集。

## 低
- 哈希口径不一致：`db.memory_add_candidate` 用原文 sha256，`dream.count` 用 `_norm` 后的哈希，所以 edit 产生的候选和 dream 候选不会去重。
- `server` 处理 edit 时分两个事务（新建候选，再 CAS 旧候选），后一步失败时会留下孤儿候选。
- `ask_sweep` 没有任何调用者；`ask_promote` 的 route 只拼进了 goal 文本，没有真正生效。

## 发行门槛
`release_check.ps1` 在 build、venv、pip 之后都没有检查 `$LASTEXITCODE`，版本号 0.6.0 写死在脚本里，最后只跑了 2 个契约测试文件。缺两项：用真实 0.5.1 DB 走一遍 v1→v5 升级的冒烟测试，以及全量 pytest。

## 已知缺口
四项都已诚实标注。漏标三项：每日 dream 只覆盖 daemon 所在的 workspace；非 git 目录下的 ask 可以写文件，只会被标记 `wrote_files`；rollback 会丢失 reindex 吸收的手改内容。

## SUMMARY
需修后合并。阻断项有两条：dream 遇到不合法的模型输出会永久卡死（H1），journal recovery 零测试（H2）。中危集中在 ask 重启后卡在 running、看板和候选 API 的回归与口径错误、adopted 没有及时落盘。release_check 还需要补退出码检查、0.5.1 DB 升级冒烟和全量测试。
