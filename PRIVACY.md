# 隐私说明

## 默认状态

- 不联网也能完成选股与回测（只要有本地数据）。
- 不上传任何数据，除非你显式开启数据回流并执行 `aq feedback send`。
- 不收集账号、持仓、成交、设备标识、IP、路径。

## 本地保存什么

配置目录默认 `~/.ashare-quant`（可用 `ASHARE_QUANT_HOME` 覆盖）：

| 文件 | 内容 |
| --- | --- |
| `config.json` | 同步区间、回测默认参数、回流开关与接收地址 |
| `market.db` | 股票列表、日线、指数日线（SQLite） |
| `feedback-preview.json` | 执行 `aq feedback preview --local` 时生成的待发送样例 |

删除整个目录即可清空全部本地数据。

## 数据回流（可选，默认关闭）

开启方式：

```bash
aq feedback policy          # 先看完整字段清单
aq feedback enable https://your-endpoint.example/feedback
```

只发送以下聚合字段：

- 程序版本、提交时间（分钟级）
- 因子名称、持仓数量、调仓频率、股票池数量
- 回测区间、交易日数、基准代码
- 年化收益、夏普、最大回撤、年化换手
- 同意条款版本

**不发送**：股票代码、名称、持仓明细、成交记录、资金规模、账号、设备、IP、本机路径、任何个人身份信息。

关闭：

```bash
aq feedback disable
```

关闭后不再发送；已发送的聚合记录无法撤回（但无法定位到个人）。

## 网络请求

| 场景 | 目标 | 何时发生 |
| --- | --- | --- |
| `aq sync` | 东方财富公开行情接口 | 你主动执行时 |
| `aq quote` | 腾讯行情公开接口 | 你主动执行时 |
| `aq doctor --network` | 上述接口连通性 | 你主动执行时 |
| `aq feedback send` | 你配置的 https 地址 | 你显式开启并执行时 |

没有后台轮询、没有遥测、没有自动更新检查。
