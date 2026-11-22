from __future__ import annotations

import pandas as pd

from ashare_quant.data.store import Store

from conftest import panel_to_long


def test_save_and_load_roundtrip(tmp_path, panel):
    store = Store(tmp_path / "t.db")
    store.save_bars(panel_to_long(panel))
    coverage = store.coverage()
    assert coverage["codes_with_bars"] == 3
    assert coverage["rows"] == panel["close"].size
    loaded = store.load_bars("600000")
    assert len(loaded) == len(panel["close"])
    assert abs(float(loaded["close"].iloc[-1]) - float(panel["close"]["600000"].iloc[-1])) < 1e-6
    store.close()


def test_panel_and_dates(tmp_path, panel):
    store = Store(tmp_path / "t.db")
    store.save_bars(panel_to_long(panel))
    start = str(panel["close"].index[100].date())
    loaded = store.panel(start=start)
    assert set(loaded) >= {"open", "close", "amount"}
    assert loaded["close"].index[0] >= pd.Timestamp(start)
    assert store.coverage()["trading_days"] == len(panel["close"])
    store.close()


def test_upsert_is_idempotent(tmp_path, panel):
    store = Store(tmp_path / "t.db")
    store.save_bars(panel_to_long(panel))
    store.save_bars(panel_to_long(panel))
    assert store.coverage()["rows"] == panel["close"].size
    store.close()


def test_stocks_meta(tmp_path):
    store = Store(tmp_path / "t.db")
    frame = pd.DataFrame({"code": ["600000"], "name": ["浦发银行"], "industry": ["银行"]})
    store.save_stocks(frame)
    assert store.stocks().iloc[0]["name"] == "浦发银行"
    store.close()
