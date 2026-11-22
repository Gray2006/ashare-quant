"""生成一份完全虚构的演示数据，便于离线体验界面与命令。

用法：
    python examples/seed_demo.py
    aq status
    aq screen "pct_chg > 1"
    aq serve

数据是随机游走生成的，股票名称也是编的，不代表任何真实标的。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ashare_quant import config
from ashare_quant.data.store import Store

INDUSTRIES = ["银行", "白酒", "电池", "半导体", "医药", "券商", "汽车", "光伏"]
NAMES = ["演示银行", "演示酒业", "演示能源", "演示芯片", "演示医药",
         "演示证券", "演示汽车", "演示光电"]


def main() -> int:
    rng = np.random.default_rng(42)
    codes = [f"60{i:04d}" for i in range(1, 31)]
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=520)
    n, m = len(dates), len(codes)
    returns = rng.normal(0.0003, 0.02, size=(n, m))
    close = pd.DataFrame(10 * np.exp(np.cumsum(returns, axis=0)), index=dates, columns=codes)
    open_ = close.shift(1).fillna(close.iloc[0]) * (1 + rng.normal(0, 0.004, (n, m)))
    volume = pd.DataFrame(rng.integers(500_000, 4_000_000, (n, m)).astype(float),
                          index=dates, columns=codes)
    long = pd.DataFrame({
        "date": np.repeat(dates.strftime("%Y-%m-%d"), m),
        "code": np.tile(codes, n),
        "open": open_.to_numpy().ravel(),
        "high": np.maximum(open_.to_numpy(), close.to_numpy()).ravel() * 1.01,
        "low": np.minimum(open_.to_numpy(), close.to_numpy()).ravel() * 0.99,
        "close": close.to_numpy().ravel(),
        "volume": volume.to_numpy().ravel(),
        "amount": (volume * close).to_numpy().ravel(),
        "pct_chg": (close.pct_change().fillna(0) * 100).to_numpy().ravel(),
        "turnover": rng.uniform(0.5, 8.0, (n, m)).ravel(),
    })
    stocks = pd.DataFrame({
        "code": codes,
        "name": [f"{NAMES[i % len(NAMES)]}{i // len(NAMES) + 1}" for i in range(m)],
        "industry": [INDUSTRIES[i % len(INDUSTRIES)] for i in range(m)],
    })
    index = pd.DataFrame({
        "code": "000300",
        "date": dates.strftime("%Y-%m-%d"),
        "open": close.mean(axis=1).to_numpy(),
        "high": close.mean(axis=1).to_numpy() * 1.005,
        "low": close.mean(axis=1).to_numpy() * 0.995,
        "close": close.mean(axis=1).to_numpy(),
        "pct_chg": close.mean(axis=1).pct_change().fillna(0).to_numpy() * 100,
    })
    with Store() as store:
        store.save_stocks(stocks)
        store.save_bars(long)
        store.save_index_bars(index)
    print(f"已生成虚构演示数据：{m} 只 × {n} 个交易日")
    print(f"配置目录: {config.home()}")
    print("下一步: aq status / aq screen \"pct_chg > 1\" / aq serve")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
