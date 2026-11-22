"""测试用合成行情，不联网。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


def make_panel(codes=("600000", "000001", "300750"), periods: int = 420, seed: int = 7):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2022-01-03", periods=periods)
    returns = rng.normal(0.0004, 0.018, size=(periods, len(codes)))
    close = pd.DataFrame(
        10.0 * np.exp(np.cumsum(returns, axis=0)),
        index=dates, columns=list(codes),
    )
    open_ = close.shift(1).fillna(close.iloc[0]) * (1 + rng.normal(0, 0.003, close.shape))
    volume = pd.DataFrame(
        rng.integers(1_000_000, 5_000_000, size=close.shape).astype(float),
        index=dates, columns=list(codes),
    )
    amount = volume * close
    pct_chg = close.pct_change().fillna(0.0) * 100.0
    turnover = pd.DataFrame(
        rng.uniform(0.5, 8.0, size=close.shape), index=dates, columns=list(codes)
    )
    high = pd.DataFrame(np.maximum(open_.to_numpy(), close.to_numpy()),
                        index=dates, columns=list(codes)) * 1.01
    low = pd.DataFrame(np.minimum(open_.to_numpy(), close.to_numpy()),
                       index=dates, columns=list(codes)) * 0.99
    return {
        "open": open_, "high": high, "low": low, "close": close,
        "volume": volume, "amount": amount, "pct_chg": pct_chg, "turnover": turnover,
    }


@pytest.fixture()
def panel():
    return make_panel()


def panel_to_long(panel: dict) -> pd.DataFrame:
    """把 {字段: DataFrame} 拼回 Store.save_bars 需要的长表。"""
    merged = None
    for field, frame in panel.items():
        long = frame.stack().rename(field).reset_index()
        long.columns = ["date", "code", field]
        merged = long if merged is None else merged.merge(long, on=["date", "code"], how="outer")
    merged["date"] = pd.to_datetime(merged["date"]).dt.strftime("%Y-%m-%d")
    return merged
