# 数据回流协议（可选）

协议标识：`ashare-quant.feedback/1`

## 原则

1. 默认关闭；
2. 只发聚合指标，不发任何明细；
3. 发送前本地校验字段白名单；
4. 地址必须是 https（本机调试允许 `http://127.0.0.1`）；
5. 用户可随时关闭，关闭后不再发送。

## 字段

```json
{
  "schema": "ashare-quant.feedback/1",
  "app_version": "0.1.0",
  "created_at": "2026-11-22T13:00:00Z",
  "consent_version": "1.0",
  "strategy": {
    "factor": "momentum_20",
    "top_n": 20,
    "rebalance": "M",
    "universe": 4800
  },
  "window": {
    "start": "2020-01-02",
    "end": "2026-11-20",
    "trading_days": 1600,
    "benchmark": "000300"
  },
  "metrics": {
    "annual_return": 0.12,
    "sharpe": 1.1,
    "max_drawdown": -0.18,
    "turnover_annual": 6.5
  }
}
```

## 明确不发送

股票代码、股票名称、持仓明细、成交记录、资金规模、账号、设备标识、IP、本机路径、任何个人身份信息。

## 本地校验

`schema.validate()` 会拒绝：

- 白名单之外的顶层字段；
- `schema` 版本不匹配；
- 缺失必填字段；
- 数值字段类型错误；
- 文本中出现 `code / holding / trade / account / email / token / path` 等禁止关键词。

## 命令

```bash
aq feedback policy                     # 查看完整说明
aq feedback status                     # 当前开关
aq feedback enable https://...         # 开启（需提供地址）
aq feedback preview --local            # 只生成本地样例，不发送
aq feedback send --local               # 本地校验但不联网
aq feedback disable                    # 关闭
```
