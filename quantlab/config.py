# -*- coding: utf-8 -*-
"""配置管理:JSON 落盘,缺失项自动补默认值。"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import shutil
import sys
from pathlib import Path

from . import secure


def _app_dir() -> Path:
    """程序目录:源码运行是仓库根;打包后是 exe 所在目录。

    PyInstaller 会把模块解到 _MEIPASS 临时目录,所以只能取 exe 本身的位置。
    注意:这里只用来「找程序」,不放用户数据 —— 见 _user_dir()。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _user_dir() -> Path:
    """用户数据目录:一个 Windows 账号一份,互相看不到。

    打包版放 %LOCALAPPDATA%\\Gray —— 同一台机器上换个人登录就是另一套
    config/data,不会串账户、串持仓、串 API key。
    源码运行仍用仓库目录,开发时不用来回找文件。

    服务端(收费接口)用 GRAY_HOME 指定独立目录,和桌面客户端完全隔离:
    同一台机器上跑服务端时,不能被客户端的 config.json 和缓存串了。
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
        if base:
            d = Path(base) / "Gray"
        else:
            d = Path.home() / "Gray"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            d = _app_dir()
        return d
    return _app_dir()


# 老目录 data/ 超过这个大小就不整份搬,只建空目录。
# 生产库 quantlab.db 已经 1.9 GB,而 tests/* 里每个用例都用
# tempfile.mkdtemp() 设 GRAY_HOME —— 一旦触发迁移,每个临时目录
# 都会拷一份 1.9 GB 的库,跑几十个用例就是几十 GB,而且全落在
# C:\Users\*\AppData\Local\Temp。2026-11-19 实测一次积了 46 GB。
_LEGACY_COPY_LIMIT = 64 * 1024 * 1024


def _migrate_legacy_config(target: Path) -> None:
    """老版本把 config.json 放在 exe 旁边,升级后自动搬一次,别让人重配。

    只在「真实用户首次运行」时才是对的:
      * GRAY_HOME 已设 = 调用方(测试/服务端)故意要一个干净独立的目录,
        往里搬旧数据既不对,又会把 GB 级的生产库复制进临时目录,直接跳过。
      * 老目录 data/ 过大时不整份复制,只建空目录 —— 那通常是行情缓存,
        不是用户配置,重建比搬便宜。
    """
    if os.environ.get("GRAY_HOME"):
        return
    if target.exists():
        return
    old = _app_dir() / "config.json"
    if not old.exists() or old.resolve() == target.resolve():
        return
    try:
        shutil.copy2(old, target)
        old_dir = _app_dir() / "data"
        dst_dir = target.parent / "data"
        if not old_dir.is_dir() or dst_dir.exists():
            return
        size = sum(f.stat().st_size for f in old_dir.rglob("*") if f.is_file())
        if size > _LEGACY_COPY_LIMIT:
            dst_dir.mkdir(parents=True, exist_ok=True)
            return
        shutil.copytree(old_dir, dst_dir)
    except Exception:
        pass


APP_DIR = _app_dir()
USER_DIR = _user_dir()
CONFIG_PATH = USER_DIR / "config.json"
DATA_DIR = USER_DIR / "data"
_migrate_legacy_config(CONFIG_PATH)

# 交易费用口径的唯一出处:模拟盘、回测、资金计划、做T都从这里取。
# 单位是小数(0.00025 = 万 2.5),不是「万分之」。
FEE_DEFAULTS = {
    "commission_rate": 0.00025,     # 佣金率(单边)
    "min_commission": 5.0,          # 单笔最低佣金(元)
    "stamp_tax_rate": 0.0005,       # 印花税(仅卖出)
    "transfer_fee_rate": 0.00001,   # 过户费(双边)
    "slippage_rate": 0.001,         # 滑点(按成交价比例)
}


DEFAULTS: dict = {
    "watchlist": ["600519", "000001", "300750", "601318", "002594", "600036", "688981", "000858"],
    "refresh_seconds": {"quote": 1.0, "market": 15, "detail": 5, "moneyflow": 20,
                        "news": 300},
    "data": {"kline_count": 260, "news_count": 20, "market_top_n": 200},
    # 本机控制接口:让 AI 代理/脚本直接操作模拟盘。
    # token 故意留空 —— 首次启动由 agent.server.ensure_token() 生成并落盘。
    # 写死在源码里的默认 token 等于没有鉴权(2026-11-22 审计 S2)。
    # 公开版默认关:没人要求开一个本地监听端口。要脚本化操作模拟盘,
    # 在 config.json 里改成 true 再重启,README「本机控制接口」一节有说明。
    "agent": {"enabled": False, "port": 8765, "token": "",
              "allow_live": False},
    # 私有自适应学习(不对外):分批执行 + 决策记忆 + 参数迭代。
    # 默认关,开了才改变下单节奏;不影响任何现有功能。
    "selflearn": {
        "enabled": False, "tranche": True, "tune": True,
        # 策略归因:由扣费后的均值/t 决定「这条策略还值不值得给它仓位」
        "attribution": True,
        # 归因判死时只写建议,打开这个才真的停新开仓
        "auto_suspend": False,
        # 目标缓动:差额不到目标的这个比例就不新开分批计划。
        # 2026-11-26 默认从 0 改成 0.15 —— 实测 603389 在 2 小时内被买 36 次、
        # 36 次全亏(均值 -5.11%),仓位 0.06 出现 24 次,就是碎单连买。
        # 0.15 的意思是「差额不到目标 15% 就不补」,不是关掉补仓。
        "target_hysteresis_pct": 0.15},
    # LLM 假设生成:给 factor_mine 提新表达式(只提假设,不碰数据/不下单)
    "hypothesis": {
        "llm": {"base_url": "", "model": "", "api_key_env": ""},
        "daily": 0},
    # 因子搜索空间预算:按天计。日线价量那条线已用 45+34 个因子证伪,
    # 默认预算 0;真挖到东西了 factor_space 会按通过率自己改判。
    "factor_mine": {
        "space_budget": {"minute_micro": 60, "financial_pit": 20,
                         "alt_data": 20, "event": 10, "daily_ohlcv": 0}},
    # 观察池:promoted 因子 → 候选策略 → 前推 + FDR → eligible → 人工提升
    "promote": {"enabled": True, "need_good": 3, "max_fails": 6,
                "horizon": 5, "top_n": 5, "min_excess_t": 2.0, "alpha": 0.05,
                "days": 250, "max_stocks": 300, "enroll_min_t": 1.0},
    # 云端研究接口(收费服务)。关掉时全部走本地,不影响任何现有功能。
    # key 不在这里,单独存 USER_DIR/cloud.key(DPAPI 加密)
    # base_url 留空 = 没配地址,不连。
    # 公开版默认关闭:PRIVACY.md 承诺「默认不收集、不上传任何个人数据」,
    # 只有用户在设置页手动开启后才可能联网(2026-11-26 复核)。
    "cloud": {"enabled": False, "base_url": "", "timeout": 30},
    # 数据回流:默认关。开启后才上传脱敏后的决策/结果/参数,不发金额和凭据。
    "feedback": {"enabled": False, "interval": 1800, "batch": 200},
    "ui": {"theme": "dark", "start_page": "quote", "font_size": 12},
    "broker": {
        # 公开版只有本地模拟撮合(README/PRIVACY 口径:不含券商、同花顺、真单)
        "mode": "sim",
        # 费率默认值和 FEE_DEFAULTS 同源,别在这儿再抄一遍
        "sim": {"initial_cash": 1_000_000.0, **FEE_DEFAULTS}},
    "risk": {
        "max_order_amount": 50_000.0,
        "max_daily_orders": 30,
        "max_price_deviation": 0.03,
        "max_position_ratio": 0.35,
        "forbid_limit_up_buy": True,
        "forbid_limit_down_sell": True,
        "allow_outside_trading_hours": False},
    "screener": {
        "presets": [
            {"name": "主力抢筹", "expr": "main_inflow > 3e7 and pct_chg > 0 and pct_chg < 7 and turnover > 1 and turnover < 15",
             "desc": "主力净流入超 3000 万、温和上涨、换手健康"},
            {"name": "缩量回踩", "expr": "pct_chg > -3 and pct_chg < 1 and volume_ratio < 0.8 and turnover < 5 and pe_ttm > 0 and pe_ttm < 60",
             "desc": "缩量、低位、估值不离谱"},
            {"name": "强势突破", "expr": "pct_chg > 4 and volume_ratio > 1.5 and turnover > 3 and market_cap > 5e9",
             "desc": "放量强势、剔除微盘"},
            {"name": "超跌反弹", "expr": "pct_chg < -3 and turnover > 2 and main_inflow > 0",
             "desc": "下跌但主力净流入"},
        ]
    },
    "backtest": {
        "start": "2022-01-01",
        "end": "",
        "initial_cash": 1_000_000.0,
        "benchmark": "000300",
        "universe": "hs300",
        "max_positions": 10,
        "per_position_pct": 0.09},
    # 按历史走势自定义策略:buy/sell 是条件表达式(见 engine/rules.py)
    "rules": [
        {"name": "均线多头", "desc": "站上 20 日线且 5 日线在 20 日线上方买入,跌破 20 日线卖出",
         "buy": "close > ma(20) and ma(5) > ma(20)", "sell": "close < ma(20)",
         "max_hold": 5, "hold_until_sell": True},
        {"name": "20日新高", "desc": "创 20 日新高买入,从高点回撤 8% 卖出",
         "buy": "close > hhv(close, 20) * 0.999", "sell": "close < hhv(close, 20) * 0.92",
         "max_hold": 5, "hold_until_sell": True},
        {"name": "放量突破", "desc": "成交量放大 1.8 倍且突破 20 日高点买入,跌破 10 日线卖出",
         "buy": "volume > vol_ma(20) * 1.8 and close > hhv(close, 20) * 0.995",
         "sell": "close < ma(10)", "max_hold": 5, "hold_until_sell": True},
    ],
    # 自动交易调度
    "autotrade": {
        "enabled": False,
        "gateway": "sim",     # sim | ths,自动交易用哪个通道
        "strategy": "momentum20",
        "pool": "hs300",
        "n_stocks": 60,
        "n_hold": 5,
        "per_position_pct": 0.18,
        "dry_run": True,
        "check_seconds": 30,
        "window_start": "14:55",
        "window_end": "15:05",
        "last_run_day": "",
        # 0 = 不限。笔数不限 = 当天能做多少单做多少单;
        # 执行次数不限 = 每次检查都重算目标(高频调仓,谨慎)
        "max_orders_per_day": 0,
        # 单票当日买入次数上限。0 = 不限。
        # 2026-11-26 默认 3 —— 实测 603389 两小时被买 36 次、36 次全亏,
        # 总笔数上限拦不住这种集中,要按票单独限。
        "max_buys_per_code_per_day": 3,
        "max_runs_per_day": 1,
        "account": "default",
        "codes": "",
        "stop_loss_pct": 0.0,
        "take_profit_pct": 0.0,
        # 全自动模式:仓位/止盈止损/回撤上限由数据自动算(engine/autopilot.py),
        # 上面两个固定百分比只在非全自动时生效
        "auto_mode": False,
        "risk_budget": 0.015,      # 单票允许贡献的日波动
        "atr_mult": 2.5,           # 止损 = N × ATR%
        "rr_ratio": 2.5,           # 盈亏比
        "max_drawdown_pct": 0.10,  # 回撤基准阈值(全自动时会分档)
        "dd_action": "halve",
        "dd_resume_ratio": 0.5,
        "adaptive_enabled": True,
        "adaptive_window": 10,
        # 资金量硬过滤(选股前先剔):单票预算不超过日均成交额的 capacity_pct,
        # 市值低于 min_market_cap、上市不满 min_list_days 的一律不选。
        # 这两条在信号层做,比让风控事后拦单便宜得多。
        "capital_filter": True,
        "capacity_pct": 0.05,
        "min_market_cap": 5e9,
        "min_list_days": 250},
    # 外盘 / 国际新闻
    "world": {"refresh_seconds": 8, "news_count": 40},
    # 走势预判:池子大小、回看长度、周期
    "forecast": {"panel_size": 60, "days": 800, "horizon": 5, "buckets": 5},
    # 新版本通知:url 指向自己托管的 update.json,留空就不检查。
    # public_key:Ed25519 公钥(base64),没有就不信任任何更新清单(2026-11-22 审计 C1)。
    "update": {"enabled": True, "url": "", "public_key": "",
               "check_on_start": True, "timeout": 5.0}}


def _safe_cloud_url(url: str) -> str:
    """云端地址只收 https 且必须有主机名。返回 "" 表示不可用。

    cloud_defaults.json 就在 exe 旁边,谁能让程序读到被改过的它,谁就能把
    客户端指到自己的服务器上 —— 于是 key、反馈、更新清单全落他手里
    (2026-11-22 审计 C6)。明文 http 直接拒绝。
    """
    from urllib.parse import urlparse
    url = (url or "").strip()
    if not url:
        return ""
    try:
        u = urlparse(url)
    except Exception:
        return ""
    if u.scheme != "https" or not u.hostname:
        return ""
    return url.rstrip("/")


_KEY_RE = re.compile(r"^gk_[0-9a-fA-F]{16}_[0-9a-fA-F]{32}$")


def _looks_like_key(text: str) -> bool:
    """gk_<16 hex>_<32 hex>:云端签发的公共 key 形状。"""
    return bool(_KEY_RE.match((text or "").strip()))


def _looks_like_update_key(text: str) -> bool:
    """Ed25519 公钥的 base64 形态:解出来必须正好 32 字节。"""
    import base64
    text = (text or "").strip()
    if not text or len(text) > 128:
        return False
    try:
        return len(base64.b64decode(text, validate=True)) == 32
    except Exception:
        return False


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def fees_of(config=None) -> dict:
    """当前生效的费用口径(config 缺省用全局配置)。

    配置里缺项、写坏、写了 NaN 都退回默认值 —— 一个脏值不该把整轮回测带崩。
    """
    try:
        raw = (config or cfg()).get("broker.sim", {}) or {}
    except Exception:
        raw = {}
    out = {}
    for key, default in FEE_DEFAULTS.items():
        try:
            v = float(raw.get(key, default))
        except (TypeError, ValueError):
            v = float(default)
        out[key] = v if math.isfinite(v) else float(default)
    return out


class Config:
    # 落盘要加密的字段(点号路径)。空值不加密;读的时候透明还原。
    SECRET_PATHS = ("agent.token",)

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
        self._decrypt_secrets()
        self._migrate_secrets()
        self._apply_cloud_defaults()

    def _apply_cloud_defaults(self) -> None:
        """发布包里的 cloud_defaults.json:首次运行注入云端地址、公共 key、更新地址。

        只在用户还没配过时生效 —— 不覆盖用户自己的设置。
        没有这个文件(源码运行)就什么都不做。

        update_url 一起注入的理由:发给别人的包,他自己不知道 update.json 在哪,
        「设置 → 关于 → 更新地址」那一栏就不该让他填。托管地址变了要重新打包,
        这比让每个用户手动复制链接可靠。
        """
        try:
            p = _app_dir() / "cloud_defaults.json"
            if not p.exists():
                return
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return
        url = _safe_cloud_url(doc.get("base_url"))
        if url and not (self.data.get("cloud") or {}).get("base_url"):
            # 只填地址,不替用户打开开关 —— 开不开是用户的决定(PRIVACY.md)
            self.set("cloud.base_url", url)
            self.save()
        key = str(doc.get("public_key") or "").strip()
        if key and _looks_like_key(key):
            try:
                from . import cloud
                if not cloud.load_key():
                    cloud.save_key(key)
            except Exception:
                pass
        upd = _safe_cloud_url(doc.get("update_url"))
        if upd and not (self.data.get("update") or {}).get("url"):
            self.set("update.url", upd)
            self.set("update.enabled", True)
            self.save()
        # 更新清单签名公钥:不给就不信任任何清单(update.check 强制 fail closed)
        pk = str(doc.get("update_public_key") or "").strip()
        if pk and _looks_like_update_key(pk) and not (self.data.get("update") or {}).get("public_key"):
            self.set("update.public_key", pk)
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        disk = copy.deepcopy(self.data)
        self._encrypt_into(disk)
        self.path.write_text(
            json.dumps(disk, ensure_ascii=False, indent=2), encoding="utf-8")

    # --- 凭据加密 ---
    def _secret_get(self, data: dict, dotted: str):
        cur = data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        return cur

    def _secret_set(self, data: dict, dotted: str, value) -> None:
        parts = dotted.split(".")
        cur = data
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = value

    def _decrypt_secrets(self) -> None:
        """把盘上的密文还原成内存里的明文;旧版明文原样保留。"""
        for path in self.SECRET_PATHS:
            val = self._secret_get(self.data, path)
            if isinstance(val, str) and secure.is_protected(val):
                self._secret_set(self.data, path, secure.unprotect_text(val))

    def _migrate_secrets(self) -> None:
        """发现旧版明文凭据就重写一次配置,把它们加密落盘。"""
        changed = False
        for path in self.SECRET_PATHS:
            val = self._secret_get(self.data, path)
            if isinstance(val, str) and val and not secure.is_protected(val):
                changed = True
                break
        if changed:
            try:
                self.save()
            except Exception:
                pass

    def _encrypt_into(self, data: dict) -> None:
        """把 data 里的凭据字段替换成密文(只改副本,不动 self.data)。"""
        for path in self.SECRET_PATHS:
            val = self._secret_get(data, path)
            if isinstance(val, str) and val and not secure.is_protected(val):
                try:
                    self._secret_set(data, path, secure.protect_text(val))
                except Exception:
                    pass

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


