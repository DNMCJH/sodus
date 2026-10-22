# 任务级评测集

一句话：每个目录是一道题，agent 只看得到 `start/`，`hidden_tests/` 在它停下之后才进场。

```
eval/tasks/<id>/
  task.json       {"id", "title", "prompt", "timeout"}
  start/          起始文件，运行前整棵拷进临时工作目录（可以是空目录）
  hidden_tests/   评测时才拷进工作目录，pytest 全绿才算 solve
```

规则：

- `prompt` 是给 agent 的完整指令，必须自己说清文件名、函数签名、输出格式和边界。
- `hidden_tests/` 里至少一个 `.py`；测试只依赖标准库和 pytest。
- 测试从工作目录导入被测代码（`import metrics`、`import shop.cart`），不要写死绝对路径。
- 一道题只考一种能力；能靠 pytest 一票判定，不靠人读代码。

跑：

```
python -m nodus bench --route opencode-glm --tasks eval/tasks --repeat 1
```

判定优先级：隐藏测试全绿 = `solve`；否则按 `timeout` → `agent_error` → `no_change` → `tests_red` 归因。
进程退出码不参与正向判定：活干成了就是干成了。
