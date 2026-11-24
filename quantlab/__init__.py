# -*- coding: utf-8 -*-
"""ashare-quant — 本地 A 股量化工作台。

模块划分:
    data    东方财富/新浪/腾讯公开行情与数据源、SQLite 存储
    engine  指标、因子、选股、回测、绩效审计、行情中枢
    ui      PySide6 界面

公开版不含交易网关、同花顺接入、加密货币与云端接口。
"""

from .version import __version__  # noqa: F401  版本号只在 version.py 维护

APP_NAME = "ashare-quant"              # 内部标识:目录名、进程名、User-Agent
APP_TITLE = "ashare-quant 量化工作台"   # 软件内显示名:标题栏、侧栏、关于
