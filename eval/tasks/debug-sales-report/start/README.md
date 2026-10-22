# 销售日报

`report_cli.py` 读一份订单行 CSV，按 region 汇总 units 和 revenue，打印 JSON。

## 输入格式

表头必须是 `region,sku,units,unit_price`；金额可以带 `$` 和千分位逗号（例如 `"$1,234.56"`）。

## 输出格式

`python report_cli.py <file.csv>` 成功时，stdout 打印 `json.dumps(report, indent=2, sort_keys=True)`，末尾一个换行，退出码 0：

```json
{
  "regions": {
    "north": {
      "revenue": 399.9,
      "units": 12
    },
    "south": {
      "revenue": 34.99,
      "units": 4
    }
  },
  "total_revenue": 434.89
}
```

`data.csv` 的期望输出就是上面这段。

文件不存在或 CSV 缺列时，stderr 打印一行以 `error: ` 开头的说明，退出码 1。

注意：现在的代码跑 `python report_cli.py data.csv` 能出 JSON，但金额和上面不一致。
