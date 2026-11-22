from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
import pytest

from ashare_quant.feedback import client, consent, schema


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ASHARE_QUANT_HOME", str(tmp_path))
    return tmp_path


def _fake_result():
    config = SimpleNamespace(top_n=20, rebalance="M", benchmark="000300")
    equity = pd.Series([1_000_000.0, 1_100_000.0],
                       index=pd.to_datetime(["2020-01-02", "2020-01-03"]))
    return SimpleNamespace(
        config=config,
        equity=equity,
        metrics={"annual_return": 0.12, "sharpe": 1.1,
                 "max_drawdown": -0.18, "turnover_annual": 6.5},
        holdings={"600519": 100},
        trades=[{"code": "600519", "price": 1700.0}],
    )


def test_disabled_by_default(home):
    assert consent.status()["enabled"] is False


def test_enable_and_disable(home):
    status = consent.enable("https://example.com/feedback")
    assert status["enabled"] is True
    assert (home / "config.json").exists()
    consent.disable()
    assert consent.status()["enabled"] is False
    assert consent.status()["endpoint"] == ""


def test_endpoint_validation(home):
    with pytest.raises(ValueError):
        consent.enable("http://insecure.example/collect")
    with pytest.raises(ValueError):
        consent.enable("")
    assert consent.enable("http://127.0.0.1:9000/feedback")["enabled"] is True


def test_payload_has_no_codes(home):
    payload = client.build_payload(_fake_result(), {"factor": "momentum_20", "universe": 100})
    text = json.dumps(payload, ensure_ascii=False)
    assert "600519" not in text
    assert "贵州茅台" not in text
    assert schema.validate(payload) == []
    assert set(payload["metrics"]) == set(schema.METRIC_KEYS)


def test_validate_rejects_extra_fields():
    payload = client.build_payload(_fake_result(), {"factor": "momentum_20", "universe": 100})
    payload["holdings"] = [{"code": "600519"}]
    errors = schema.validate(payload)
    assert errors
    assert any("未授权字段" in error for error in errors)


def test_validate_rejects_short_window():
    payload = client.build_payload(_fake_result(), {"factor": "momentum_20", "universe": 100})
    payload["window"]["trading_days"] = "many"
    assert schema.validate(payload)
