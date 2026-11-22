# ashare-quant

A 股行情分析、因子选股与回测工作台。**只做研究与教育，不接实盘、不自动下单、不碰加密货币。**

```
行情同步 → 本地 SQLite → 因子计算 → 条件选股 → 回测 → 本地仪表盘
```

## 边界（先看这段）

这个项目从设计上就砍掉了以下内容，不是"默认关闭"而是**没有实现**：

| 不做 | 原因 |
| --- | --- |
| 加密货币行情/交易 | 与 A 股研究无关，且境内监管风险高 |
| 券商/同花顺自动下单 | 无交易通道，不接任何下单接口 |
| 模拟盘伪装实盘 | 不提供"一键跟单""托管"类功能 |
| 抓取付费/需登录数据 | 只用公开行情接口 |
| 上传持仓、代码、成交 | 数据回流只发聚合指标，默认关闭 |
| 投资建议 | 输出的是统计结果，不是买卖建议 |

完整说明见 [DISCLAIMER.md](DISCLAIMER.md) 与 [PRIVACY.md](PRIVACY.md)。

## 与本地完整版隔离

本仓库是**独立公开版**，与作者本地的完整版量化终端（含 GUI、自动交易、同花顺接入）完全隔离：

- 不共享代码、数据库、配置目录、行情缓存与数据回流端点；
- 两边互不 import、互不读写对方目录；
- 公开仓库不包含私有策略、参数、账户信息或内部文档；
- 只参考思路、重新实现，不整段搬运。

如果你 fork 本项目，也建议给它独立的虚拟环境与 `ASHARE_QUANT_HOME`，避免和别的工具串数据。

## 功能

- **行情缓存**：东方财富日线 + 腾讯实时快照，落本地 SQLite；增量同步、断点续传、限速与熔断。
- **技术指标**：MA、EMA、RSI、MACD、ATR、布林带、滚动波动率、最大回撤。
- **因子库**：动量、反转、波动率、换手、量比、均线乖离、RSI、MACD、ATR、布林宽度、成交额、市值、PE/PB 等 15 个因子。
- **安全选股表达式**：自写 AST 求值器，白名单节点，禁用属性访问/下标/导入/`eval`。
- **回测**：T+1 开盘成交、整手、佣金最低 5 元、印花税、滑点、涨跌停不可成交、停牌跳过、等权调仓。
- **报告**：Markdown + 内联 SVG 净值曲线，可离线打开。
- **本地仪表盘**：`aq serve` 打开 `127.0.0.1` 网页，看概览、选股、回测，无外部 CDN。
- **数据回流（可选）**：默认关闭，只上传聚合回测指标，开启前展示完整字段清单。

## 安装

需要 Python 3.10+。

```bash
git clone https://github.com/Gray2006/ashare-quant.git
cd ashare-quant
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -e .
```

开发依赖：

```bash
pip install -e ".[dev]"
pytest
```

## 快速开始

```bash
# 1. 初始化（配置目录默认 ~/.ashare-quant）
aq init

# 2. 先同步 300 只股票，确认接口可用
aq sync --days 400 --limit 300

# 3. 看本地数据覆盖
aq status

# 4. 选股
aq screen "pct_chg > 1 and between(turnover, 1, 15) and close > 5" --limit 20

# 5. 回测
aq backtest --factor momentum_20 --top 20 --rebalance M --start 2020-01-01

# 6. 打开本地仪表盘
aq serve
```

常用命令：

| 命令 | 作用 |
| --- | --- |
| `aq init` | 建配置目录与数据库 |
| `aq sync [--days N] [--limit N] [--full]` | 同步股票列表、日线、基准指数 |
| `aq status` | 数据覆盖、配置、回流状态 |
| `aq quote 600519 000001` | 实时快照（仅查询，不交易） |
| `aq screen "<表达式>"` | 条件选股 |
| `aq factors` | 列出因子及方向 |
| `aq backtest --factor ...` | 因子回测 |
| `aq report --out report.md` | 生成 Markdown 报告 |
| `aq serve [--port 8765]` | 本地仪表盘 |
| `aq feedback status/preview/...` | 数据回流（默认关闭） |
| `aq doctor` | 自检（可选联网测试接口） |

运行 `aq <命令> --help` 查看全部参数。

## 选股表达式

只允许字段、数字、比较、`and/or/not` 与白名单函数：

```text
pct_chg > 1 and between(turnover, 1, 15)
rank(momentum_20) > 0.8 and volatility_20 < 0.4
between(pe_ttm, 0, 40) and market_cap > 5000000000
amount > 100000000 and abs(pct_chg) < 3
```

函数：`abs min max round log sqrt sign rank zscore clip between isna notna`。

字段来自本地最新快照与因子；加 `--live` 会用在线快照补 PE/PB/市值/量比/振幅/行业。

## 数据来源

- 东方财富公开行情接口：股票列表、日线、指数日线、实时快照。
- 腾讯行情公开快照：单只/多只实时报价。

两者都是公开网页接口，不是官方 API，可能变更或限流。项目内置 0.15s 最小间隔、指数退避重试、连续失败熔断，并对返回结构做宽松解析。

**不附带、不再分发任何行情数据**。数据库只存在你的机器上。

详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 目录结构

```
ashare_quant/
  config.py         配置与路径
  utils.py          代码/市场/日期工具
  data/             接口客户端、SQLite、同步
  engine/           指标、因子、选股、回测、指标计算、报告
  feedback/         可选数据回流（同意、schema、客户端）
  web/              本地仪表盘（标准库 HTTP）
  cli.py            命令行
tests/              离线合成数据测试
docs/               架构与协议说明
```

## 数据与隐私

- 默认完全离线可用，只有 `sync` / `quote` / `feedback send` 会联网。
- 配置目录：`~/.ashare-quant`，可用 `ASHARE_QUANT_HOME` 覆盖。
- 不上传代码、持仓、成交、账号、设备信息。
- 数据回流默认关闭，见 [PRIVACY.md](PRIVACY.md)。

## 开发

```bash
pytest                 # 全部离线测试
pytest --cov=ashare_quant
```

CI 模板见 `docs/github-actions-tests.yml`，复制到 `.github/workflows/tests.yml` 即可启用（需要 token 具备 `workflow` scope）。

提交前请确认：不新增交易/下单接口，不引入需要登录的数据源，不把密钥写进代码。

## 许可证

MIT，见 [LICENSE](LICENSE)。

## 免责声明

本项目仅用于技术学习与量化研究，不构成任何投资建议。历史回测不代表未来收益。使用本项目产生的任何决策与后果由使用者自行承担。详见 [DISCLAIMER.md](DISCLAIMER.md)。
