from __future__ import annotations

import pandas as pd

from ashare_quant import cli


def test_parser_builds():
    parser = cli.build_parser()
    args = parser.parse_args(["factors"])
    assert args.command == "factors"


def test_main_factors(capsys):
    code = cli.main(["factors"])
    out = capsys.readouterr().out
    assert code == 0
    assert "momentum_20" in out


def test_main_without_command(capsys):
    code = cli.main([])
    assert code == 1
    assert "usage" in capsys.readouterr().out.lower()


def test_status_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ASHARE_QUANT_HOME", str(tmp_path))
    code = cli.main(["status"])
    out = capsys.readouterr().out
    assert code == 0
    assert "数据回流: 关闭" in out


def test_backtest_parser():
    parser = cli.build_parser()
    args = parser.parse_args(["backtest", "--factor", "momentum_20", "--top", "10"])
    assert args.top == 10
    assert args.factor == "momentum_20"
