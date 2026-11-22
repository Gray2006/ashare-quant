from __future__ import annotations

import json

import pandas as pd
import pytest

from ashare_quant.data.store import Store
from ashare_quant.web import server

from conftest import make_panel, panel_to_long


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    monkeypatch.setenv("ASHARE_QUANT_HOME", str(tmp_path))
    panel = make_panel(periods=300)
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=300)
    panel = {key: frame.set_axis(dates) for key, frame in panel.items()}
    with Store(tmp_path / "market.db") as store:
        store.save_bars(panel_to_long(panel))
        store.save_stocks(pd.DataFrame({
            "code": list(panel["close"].columns),
            "name": ["浦发银行", "平安银行", "宁德时代"],
            "industry": ["银行", "银行", "电池"],
        }))
        index = pd.DataFrame({
            "open": panel["open"]["600000"],
            "high": panel["high"]["600000"],
            "low": panel["low"]["600000"],
            "close": panel["close"]["600000"],
            "pct_chg": panel["pct_chg"]["600000"],
        })
        store.save_index_bars(index.reset_index(names="date").assign(code="000300"))
    return tmp_path


def test_overview(seeded):
    data = server.overview()
    assert data["coverage"]["codes_with_bars"] == 3
    assert len(data["indices"]) == 1
    assert data["gainers"]


def test_screen_endpoint(seeded):
    data = server.run_screen("close > 0", limit=2)
    assert data["count"] == 2
    assert "rows" in data


def test_screen_rejects_bad_expression(seeded):
    data = server.run_screen("__import__('os')")
    assert "error" in data


def test_backtest_endpoint(seeded):
    data = server.run_backtest({
        "factor": "momentum_20", "top": 2, "rebalance": "M",
        "start": "2022-01-01", "end": None,
    })
    assert "error" not in data, data
    assert data["curve"]
    assert "sharpe" in data["metrics"]
    json.dumps(data, allow_nan=False)


def test_backtest_unknown_factor(seeded):
    data = server.run_backtest({"factor": "nope"})
    assert "error" in data


def test_clean_handles_nan():
    assert server._clean(float("nan")) is None
    assert server._clean({"a": float("inf")}) == {"a": None}
