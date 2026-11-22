# -*- coding: utf-8 -*-
"""配置管理:JSON 落盘,缺失项自动补默认值。

公开版只保留行情、自选、界面、缓存相关的配置;不含交易通道、
加密货币、云端凭据与自动更新配置。
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path


def _app_dir() -> Path:
    """程序目录:源码运行是仓库根;打包后是 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _user_dir() -> Path:
    """用户数据目录:一个 Windows 账号一份。

    打包版放 %LOCALAPPDATA%\\Gray,源码运行放仓库目录。
    设 GRAY_HOME 可指定独立目录(测试/便携版)。
    """
    env = os.environ.get("GRAY_HOME")
    if env:
        d = Path(env)
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return d
    if getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        d = Path(base) / "ashare-quant" if base else Path.home() / "ashare-quant"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            d = _app_dir()
        return d
    return _app_dir()


APP_DIR = _app_dir()
USER_DIR = _user_dir()
CONFIG_PATH = USER_DIR / "config.json"
DATA_DIR = USER_DIR / "data"

DEFAULTS: dict = {
    "watchlist": ["600519", "000001", "300750", "601318", "002594",
                  "600036", "688981", "000858"],
    "refresh_seconds": {"quote": 1.0, "market": 15, "detail": 5,
                        "moneyflow": 20, "news": 300},
    "data": {"kline_count": 260, "news_count": 20, "market_top_n": 200},
    "offline": False,
    "ui": {"theme": "dark", "start_page": "quote", "font_size": 12},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else CONFIG_PATH
        self.data = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.data = _deep_merge(DEFAULTS, raw)
            except Exception:
                self.data = copy.deepcopy(DEFAULTS)
        else:
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    # --- 便捷访问 ---
    def get(self, dotted: str, default=None):
        cur = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def set(self, dotted: str, value, save: bool = False) -> None:
        parts = dotted.split(".")
        cur = self.data
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value
        if save:
            self.save()

    @property
    def watchlist(self) -> list[str]:
        return list(self.data.get("watchlist", []))

    def set_watchlist(self, codes: list[str]) -> None:
        self.data["watchlist"] = codes
        self.save()


_cfg: Config | None = None


def cfg() -> Config:
    global _cfg
    if _cfg is None:
        _cfg = Config()
    return _cfg
