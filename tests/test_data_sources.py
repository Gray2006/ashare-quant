from __future__ import annotations

import pandas as pd

from ashare_quant.data import sina, tencent


class FakeKlineClient:
    """按 end 参数返回两页，模拟腾讯 640 根翻页。"""

    def __init__(self):
        self.calls = 0

    def get_json(self, url, params=None):
        self.calls += 1
        end = params["param"].split(",")[3]
        if end >= "2024-01-10":
            rows = [
                ["2024-01-08", "10.0", "10.5", "10.6", "9.9", "1000"],
                ["2024-01-09", "10.5", "10.4", "10.7", "10.2", "1200"],
                ["2024-01-10", "10.4", "10.8", "10.9", "10.3", "1500"],
            ]
        else:
            rows = [
                ["2024-01-03", "9.5", "9.8", "9.9", "9.4", "800"],
                ["2024-01-04", "9.8", "10.0", "10.1", "9.7", "900"],
                ["2024-01-05", "10.0", "10.2", "10.3", "9.9", "1100"],
            ]
        return {"code": 0, "data": {"sh600519": {"qfqday": rows}}}


def test_tencent_kline_pages_and_scales():
    client = FakeKlineClient()
    frame = tencent.kline("600519", start="2024-01-03", end="2024-01-10", client=client)
    assert client.calls == 2
    assert list(frame["date"]) == ["2024-01-03", "2024-01-04", "2024-01-05",
                                   "2024-01-08", "2024-01-09", "2024-01-10"]
    assert frame["code"].iloc[0] == "600519"
    # 手 -> 股
    assert frame["volume"].iloc[0] == 80000.0
    assert frame["pct_chg"].iloc[0] != frame["pct_chg"].iloc[0]  # 首行 NaN
    assert abs(frame["pct_chg"].iloc[1] - (10.0 / 9.8 - 1) * 100) < 1e-9


class FakeSinaClient:
    def __init__(self):
        self.pages = []

    def get_json(self, url, params=None):
        self.pages.append(params["page"])
        if params["page"] == 1:
            return [
                {"symbol": "sh600000", "code": "600000", "name": "浦发银行",
                 "trade": "7.10", "changepercent": "1.2", "per": "5.0",
                 "pb": "0.6", "turnoverratio": "0.8", "mktcap": "2000000",
                 "nmc": "1900000"},
                {"symbol": "sz000001", "code": "000001", "name": "平安银行",
                 "trade": "10.20", "changepercent": "-0.5", "per": "4.5",
                 "pb": "0.5", "turnoverratio": "1.1", "mktcap": "3000000",
                 "nmc": "2900000"},
            ]
        return []


def test_sina_list_stocks():
    client = FakeSinaClient()
    frame = sina.list_stocks(client=client)
    assert client.pages == [1]  # 不足一页即结束
    assert list(frame["code"]) == ["600000", "000001"]
    assert frame["name"].iloc[0] == "浦发银行"
    assert frame["market_cap"].iloc[0] == 2000000 * 1e4
