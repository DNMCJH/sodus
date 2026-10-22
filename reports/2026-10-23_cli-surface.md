# Nodus CLI 面：46 个命令，哪些该留下

口径：docs = README+docs 里出现次数；tests = 测试里被当参数传过；code = 源码里被字符串引用。

| 命令 | 实现文件 | docs | tests | code |
|---|---|---|---|---|
| `desk` | system.py | 0 | 0 | 1 |
| `down` | system.py | 0 | 0 | 1 |
| `jev` | system.py | 0 | 0 | 1 |
| `tasks` | tasks.py | 0 | 0 | 3 |
| `balance` | system.py | 0 | 1 | 3 |
| `handoff` | tasks.py | 1 | 0 | 9 |
| `pipelines` | pipeline.py | 1 | 0 | 1 |
| `probe` | system.py | 1 | 0 | 3 |
| `serve` | system.py | 1 | 0 | 2 |
| `show` | tasks.py | 1 | 0 | 7 |
| `triage` | decisions.py | 1 | 0 | 2 |
| `unlock` | system.py | 1 | 0 | 1 |
| `brief` | sessions.py | 2 | 0 | 2 |
| `doctor` | system.py | 2 | 0 | 1 |
| `fix` | pipeline.py | 2 | 1 | 5 |
| `merge` | proposals.py | 3 | 0 | 5 |
| `proposal` | proposals.py | 2 | 1 | 8 |
| `proposals` | proposals.py | 2 | 1 | 6 |
| `report` | pipeline.py | 2 | 1 | 3 |
| `review` | pipeline.py | 2 | 1 | 13 |
| `routes` | topology.py | 3 | 0 | 5 |
| `topology` | topology.py | 3 | 0 | 1 |
| `watch` | tasks.py | 3 | 0 | 1 |
| `decisions` | system.py | 1 | 3 | 4 |
| `locks` | system.py | 4 | 0 | 2 |
| `providers` | topology.py | 4 | 0 | 9 |
| `reconcile` | system.py | 4 | 0 | 1 |
| `task` | tasks.py | 2 | 2 | 22 |
| `mcp` | system.py | 5 | 0 | 1 |
| `sessions` | sessions.py | 4 | 1 | 6 |
| `stop` | tasks.py | 4 | 1 | 1 |
| `branch` | sessions.py | 3 | 3 | 15 |
| `reject` | proposals.py | 3 | 3 | 3 |
| `profiles` | topology.py | 4 | 3 | 6 |
| `up` | system.py | 3 | 4 | 4 |
| `agents` | system.py | 1 | 7 | 5 |
| `route` | decisions.py | 2 | 7 | 23 |
| `verify` | pipeline.py | 3 | 7 | 15 |
| `tdd` | pipeline.py | 9 | 2 | 5 |
| `session` | sessions.py | 4 | 8 | 20 |
| `resume` | sessions.py | 9 | 5 | 12 |
| `runs` | tasks.py | 5 | 10 | 26 |
| `run` | tasks.py | 13 | 9 | 26 |
| `cost` | ? | 3 | 24 | 33 |
| `usage` | system.py | 4 | 29 | 31 |
| `status` | system.py | 1 | 42 | 57 |
