# 运行手册 供应商主数据台账

## 启动

```bash
docker compose up -d --build
curl -s http://127.0.0.1:18205/api/health
```

首次打开任意页面会按内置样例自动采信一次，随后逐级计算归一名、分块、候选对与合并簇。

## 重建数据库

```bash
docker compose down -v
docker compose up -d --build
```

`-v` 会删除命名卷 `merge-data`，批次表与复核决定一并清空。

## 重跑与幂等

- `POST /api/suppliers/import` 带 `force: true` 时按同一批文件重算。
- 批次号由输入摘要（按文件名排序的 sha256 拼接）与规则版本共同决定，同一批文件得到同一个 `run_id`；
  命中已有批次时直接复用结果，不新增记录。
- 复核决定写入后，簇与金额会立即重算，不需要手工触发。

## 常用请求

```bash
curl -s -X POST http://127.0.0.1:18205/api/suppliers/import -H 'Content-Type: application/json' -d '{}'
curl -s http://127.0.0.1:18205/api/reconcile
curl -s http://127.0.0.1:18205/api/export/master.csv -o master.csv
curl -s http://127.0.0.1:18205/api/export/manifest.json -o manifest.json
```

判定一张复核单：

```bash
curl -s -X POST http://127.0.0.1:18205/api/reviews/P-00105/decision \
  -H 'Content-Type: application/json' \
  -d '{"decision":"merge","expected_version":0}'
```

## 付款批次与抵扣核销

```bash
# 导入付款批次（应付 + 退款抵扣），同批文件得到同一个 PAY- 批次号
curl -s -X POST http://127.0.0.1:18205/api/payments/import -H 'Content-Type: application/json' -d '{}'
# 看待核抵扣与阻断原因
curl -s http://127.0.0.1:18205/api/payments/pending
# 核销一笔抵扣（expected_version 取页面/接口当前版本，首次为 0）
curl -s -X POST http://127.0.0.1:18205/api/payments/PL-94CB6574C9D3F677/writeoff \
  -H 'Content-Type: application/json' \
  -d '{"action":"approve","expected_version":0}'
# 三处口径对账
curl -s http://127.0.0.1:18205/api/payments/reconcile
# 导出主体净额、付款行与付款隔离区
curl -s http://127.0.0.1:18205/api/payments/export/clusters.csv -o pay-clusters.csv
```

- 抵扣核销前一律不进可付净额；核销后只在该抵扣锁定的主体簇内抵减，不跨主体。
- `force: true` 重跑替换付款行但保留核销决定，已确认归属与净额不变、不重复计入。
- 税号冲突簇的抵扣为阻断态，`approve` 会被拒，需先在主数据层解除冲突或 `reject`。

## 失败码处理

| 原因码 | 处理 |
|---|---|
| `E_FILE_MISSING` | 检查 `sample_data/` 下的文件名与大小写 |
| `E_MISSING_COLUMN` | 表头缺必填列，按列契约补列后重试 |
| `E_DECODE_FAILED` | 文件既非 UTF-8 也非 GB18030，需转码 |
| `Q_ROW_SHORT` / `Q_ROW_LONG` | 该行进隔离区，修正分隔符后重跑 |
| `V_STALE_DECISION` | 页面停留过久，刷新后按新版本再提交 |
| `V_BAD_DECISION` | 税号冲突的候选对不允许合并；或对非抵扣行发起核销 |
| `V_UNKNOWN_PAIR` | 候选对编号不属于当前批次，刷新页面 |
| `W_SUPPLIER_UNKNOWN` | 付款供应商号不在主数据，先补名录或退回业务更正 |
| `W_CLUSTER_CONFLICT` | 主体税号冲突未解，阻断抵扣核销与净额抵减 |
| `W_DEDUCTION_PENDING` | 抵扣待核销，暂不抵减，属正常待办而非错误 |
| `W_NAME_MISMATCH` | 付款名称与主数据不符，应付只预警，需人工核对 |
| `W_LINE_KEY_DUP` | 付款行内容指纹重复，后者隔离，核查是否重复报送 |

## 测试

```bash
docker compose exec -T app python -m pytest -q
```
