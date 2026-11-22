# 架构

## 数据流

```
公开行情接口
  ├─ 东方财富：股票列表 / 日线 / 指数日线 / 实时快照
  └─ 腾讯：实时报价
        │
        ▼
data/http.py      限速、重试、熔断、诚实 UA
        │
        ▼
data/*.py         解析成统一长表
        │
        ▼
data/store.py     SQLite：stocks / daily / index_daily / meta
        │
        ├─ engine/indicators.py   技术指标
        ├─ engine/factors.py      横截面因子
        ├─ engine/screener.py     AST 安全表达式
        ├─ engine/backtest.py     T+1 组合回测
        ├─ engine/report.py       Markdown + SVG
        └─ web/server.py          本地仪表盘（只读 + 内存回测）
```

## 模块职责

| 模块 | 职责 | 不做什么 |
| --- | --- | --- |
| `config.py` | 路径、默认参数、JSON 配置 | 不存密钥 |
| `utils.py` | 代码归一、市场判断、日期 | 不做 IO |
| `data/http.py` | 会话、限速、重试、熔断 | 不解析业务字段 |
| `data/eastmoney.py` | 东财接口与字段映射 | 不落库 |
| `data/tencent.py` | 腾讯快照解析 | 不落库 |
| `data/store.py` | SQLite 读写、面板拼装 | 不算因子 |
| `data/sync.py` | 增量同步编排 | 不生成信号 |
| `engine/indicators.py` | 单标的指标 | 不做横截面 |
| `engine/factors.py` | 因子注册与计算 | 不做回测 |
| `engine/screener.py` | 表达式求值与筛选 | 不执行任意代码 |
| `engine/backtest.py` | 组合回测与成本 | 不接实盘 |
| `engine/metrics.py` | 收益/风险指标 | 不画图 |
| `engine/report.py` | 报告输出 | 不联网 |
| `feedback/` | 可选聚合回流 | 默认关闭、不发明细 |
| `web/` | 本地只读界面 | 不监听公网 |

## 关键口径

- **复权**：日线使用前复权（东财 `fqt=1`），因子与回测都在同一口径上。
- **成交**：信号日收盘后生成目标，下一交易日开盘成交；涨停不买、跌停不卖、停牌跳过。
- **成本**：佣金双边、最低 5 元；印花税仅卖出；滑点双边。
- **整手**：买入按 100 股向下取整。
- **基准**：默认沪深 300，按区间首日归一化。
- **股票池**：剔除 ST、低价、低流动性（20 日平均成交额下限）。

## 隔离

公开版与作者本地完整版是两套独立代码库：不共享模块、数据库、配置目录与回流端点。详见 README「与本地完整版隔离」。
