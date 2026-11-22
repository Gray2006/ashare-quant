from __future__ import annotations

from datetime import date

from ashare_quant.utils import (
    date_str,
    is_index,
    market_of,
    normalize_code,
    secid,
    tx_symbol,
)


def test_normalize_code():
    assert normalize_code("sh600519") == "600519"
    assert normalize_code("600519.SH") == "600519"
    assert normalize_code(" 000001 ") == "000001"
    assert normalize_code(None) == ""


def test_market_and_symbols():
    assert market_of("600519") == "SH"
    assert market_of("000001") == "SZ"
    assert market_of("830799") == "BJ"
    assert is_index("000300")
    assert not is_index("600519")
    assert secid("600519") == "1.600519"
    assert secid("000001") == "0.000001"
    assert secid("000300", index=True) == "1.000300"
    assert tx_symbol("600519") == "sh600519"
    assert tx_symbol("300750") == "sz300750"


def test_dates():
    assert date_str(date(2026, 11, 22)) == "2026-11-22"
    assert date_str("20261122") == "2026-11-22"
    assert date_str("") == ""
