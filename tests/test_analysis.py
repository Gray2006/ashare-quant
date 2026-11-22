from __future__ import annotations

from ashare_quant.data.store import Store
from ashare_quant.engine import analysis

from conftest import panel_to_long


def test_analyze_summary_and_series(tmp_path, panel):
    store = Store(tmp_path / "t.db")
    store.save_bars(panel_to_long(panel))
    result = analysis.analyze(store, "600000", days=200)
    assert "error" not in result
    summary = result["summary"]
    assert summary["code"] == "600000"
    assert summary["trend"] in ("多头", "空头", "震荡")
    assert isinstance(summary["signals"], list)
    assert summary["ma20"] is not None
    assert summary["rsi14"] is not None
    assert 0 <= summary["rsi14"] <= 100
    assert summary["bars"] == len(panel["close"])
    assert len(result["series"]) == 200
    latest = result["series"][-1]
    assert {"date", "close", "ma5", "dif", "rsi"} <= set(latest)
    text = analysis.format_summary(result)
    assert "600000" in text and "信号" in text
    store.close()


def test_analyze_missing_code(tmp_path):
    store = Store(tmp_path / "t.db")
    result = analysis.analyze(store, "999999")
    assert "error" in result
    store.close()
