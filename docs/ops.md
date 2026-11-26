# 运维手册(每天看什么、出问题怎么查)

## 一、自动跑的东西

| 时间 | 任务 | 做什么 | 日志 |
|---|---|---|---|
| 交易日 15:40 | 云端 `daily_signal.py` | 收盘后算信号(不跑则 `/v1/signal/daily` 503) | `/var/log/gray-signal.log` |
| 交易日 16:05 | 云端 `collect_data.py` | 采日线快照 / 龙虎榜 / 新闻 / 5 分钟线 | `data/logs/collect.log`、`data/collect/last_run.json` |
| 交易日 16:30 | 云端 `learn_daily.py` | 预测 + 回填 + 参数建议 + 日报 | `/var/log/gray-learn.log` |
| 每天 19:00 | 本机 `daily_maintain.py` | 快照 / 龙虎榜 / 因子重审 / 自检 / 5 分钟线 | `logs/maintain-YYYY-MM-DD.log` |
| 每天 21:30 | 本机 `daily_maintain.py` | 同上(第二次,补漏) | 同上 |
| 每 5 分钟 | 云端 `gray-watchdog.timer` | `/health` 不通就重启服务 | `journalctl -u gray-service` |

## 一之二、自动交易本身没有计划任务

计划任务里只有 `GrayQuant_LHB_1900/2130`(跑 `daily_maintain.py`)和 `Gray-TrainingExport`。
**自动交易不会被系统定时触发** —— 只有两种情况会下单:

1. 界面「立即执行一次」
2. `AutoPilot` 在程序开着、且当前时间落在执行窗口(默认 14:55~15:05)时自己跑

所以「策略被判 suspend、只平不新开」这件事,只有在真的执行时才会体现。
要给自动交易加定时,得新建一个计划任务指向 pilot,这是改交易行为,需要你确认。

## 二、早上先跑这两条

```bash
python tools/preopen_check.py                  # 开盘前 5 项 PASS/WARN
python tools/learn_status.py --account 默认账户  # 闭环体检
```

`preopen_check` 看:日线是否到上一交易日、通道是否配好、dry_run、自学习参数、今日决策数。
WARN 不代表坏,比如开盘前数据当然只有昨天的。

按段读:

- **归因**:`suspend` 表示策略扣费后显著为负,仓位乘数 0.5;`insufficient` 是样本 <12,别下结论
- **预测层**:括号里是去重后条数;准确率 <45% 会让仓位再乘 0.8
- **搜索空间**:`dead` = 测过 ≥30 个唯一因子且 0 通过,预算归零;`unknown` = 样本还不够
- **观察池**:0 候选是常态,有候选才需要人工确认提升
- **实盘 vs 基准**:账户总权益(含现金)对比沪深300,别看单笔均值
- **自学习参数**:这里显示的是 config.json 覆盖后的**生效值**,不是代码默认值
- **口径漂移**:config 策略 ≠ 库里最近写库策略时会给 ⚠

## 三、常见异常

| 现象 | 先查 | 处理 |
|---|---|---|
| 日报「今日」0 条 | 体检页「口径漂移」段 | 看是不是策略名不一致;`daily_report` 有自愈,但根因要修写库端 |
| 夜间日志停在某一步 | `logs/maintain-*.log` 里 `✗` 行 | 单步失败已隔离,其余步骤会继续;查那一步的数据源 |
| 采集 job 退出码 2 | `last_run.json` 的 `errors` | 多半是云端缺 `quantlab/` 或 `tools/`,见 deploy/README |
| `/v1/learn/*` 403 | key 的 subject | 默认本人 subject 是 `me`;用别的名字要在 env 配 `GRAY_OWNER_SUBJECTS` |
| 观察池一直空 | 空间账本 | 正常;要入池得先有因子过双 t |
| 资金流失败 | — | push2his 被掐,已知问题,别重试太频繁 |

## 四、开关与回滚

```bash
# 关掉分批(回到一次性下单)
python -c "from quantlab.config import cfg; cfg().set('selflearn.tranche', False, save=True)"

# 同一天不重开计划的容差(0 = 每次目标变就重开,不建议)
python -c "from quantlab.config import cfg; cfg().set('selflearn.replan_tolerance', 0.05, save=True)"

# 目标缓动(差额不到目标的这个比例就不补)
python -c "from quantlab.config import cfg; cfg().set('selflearn.target_hysteresis_pct', 0.0, save=True)"

# 归因判死后真的停新开仓(2026-11-26 已开启:multifactor 判 suspend → 只平不新开)
python -c "from quantlab.config import cfg; cfg().set('selflearn.auto_suspend', True, save=True)"
```

## 四之二、策略被判死后会冻结多久

归因看最近 `selflearn.attribution_days` 天(默认 90)。判 suspend 且开了
`auto_suspend` 后,这条策略只平不新开;要等这批亏损样本滑出窗口、样本数掉到 12 以下
(归因变 `insufficient`)才会自动解冻 —— 默认大约三个月。

想提前重试:

```bash
# 缩短窗口(更激进的复核,亏损样本会更快滑出)
python -c "from quantlab.config import cfg; cfg().set('selflearn.attribution_days', 30, save=True)"
# 或临时关掉自动暂停(只写建议)
python -c "from quantlab.config import cfg; cfg().set('selflearn.auto_suspend', False, save=True)"
```

## 五、测试

```bash
python -m pytest tests -q          # 全量(约 80s)
python -m pytest gray-service/tests -q
```

脚本式测试(会写库的)单独跑,不要塞进 pytest:
`python tests/test_selflearn.py`、`python tests/test_promote.py`、`python tests/test_strategy_review.py`

## 六、云端部署

```bash
sudo bash deploy/install.sh    # 装服务 + 采集 timer + 守护 timer,最后自检 /health
```

前提:`/opt/gray-service` 下同时有 `gray-service/`、`quantlab/`、`tools/`。


## 三、已知风险:回撤闸可能永久停手

`adaptive.DrawdownGuard` 的峰值计算是:

```python
peak = max(peak, equity)      # 只增不减
dd = 1 - equity / peak
```

一旦触发(`dd >= max_pct`),解除条件是 `dd <= max_pct * resume_ratio`。
默认 `max_pct=0.10`、`resume_ratio=0.5`,也就是 **dd 要回到 5% 以下**。

**问题**:峰值是历史最高,不随时间衰减。举例:

| 权益 | 峰值 | dd | 状态 |
|---|---|---|---|
| 100 万 | 100 万 | 0% | 正常 |
| 85 万 | 100 万 | 15% | **触发,停新仓** |
| 92 万 | 100 万 | 8% | 仍在停(8% > 5%) |
| 96 万 | 100 万 | 4% | 解除 |

**要涨回峰值的 95% 才解除。** 如果账户从 100 万跌到 85 万,
之后长期在 90 万附近震荡,就永远解不了 —— 等于「一次大回撤,永久停手」。

### 三个可选改法(未实施,等主人定)

1. **滚动窗口峰值**:`peak = max(最近 60 天权益)`,老高点自然过期
2. **时间衰减**:每过 N 天把 peak 往当前权益方向拉一点
3. **定期重置**:每月 1 号 `reset()` 一次

**为什么没改**:这是实盘保护逻辑,放宽了会带来真实风险。
而且当前实盘数据(胜率 33%、均值 −4.5%)本来就不该加仓 ——
停手在现阶段是对的。

**什么时候该改**:等策略转正、或者你确认「停手」已经影响到正常调仓。

### 怎么手动解除

```python
from quantlab.engine.adaptive import DrawdownGuard
DrawdownGuard("你的账户名").reset()
```

或者界面上找「重置回撤闸」。


## 四、改配置默认值为什么经常不生效

踩过三次,根因链是这样的:

```
config.py 的 DEFAULTS 改了
  ↓
cfg() 是 _deep_merge(DEFAULTS, raw)  ← 深合并
  ↓
用户 config.json 里显式写了旧值
  ↓
深合并时用户值覆盖默认值 → 改 DEFAULTS 等于白改
```

**具体例子**(2026-11-26):`target_hysteresis_pct` 从 0.0 改成 0.15,

1. 改 `DEFAULTS` → `config.json` 里有 `0.0` 显式值,覆盖了 → 无效
2. 改读取处 `cfg.get(key, 0.15)` → 用户配置有值,还是 0.0 → 无效
3. **改 `config.json` 本身** → 生效

### 改默认值时的正确顺序

1. 改 `DEFAULTS`
2. 跑 `python tools/check_config_defaults.py` 确认读取处没硬编码
3. **检查用户 `config.json` 有没有显式写这个 key** —— 有就一起改
4. 改完跑 `python tools/learn_status.py` 或界面确认实际生效值

第 3 步最容易漏。配置文件在 `.gitignore` 里,代码改了它不会跟着变。

### 怎么快速查

```bash
python -c "import json; c=json.load(open('config.json',encoding='utf-8')); print(c.get('selflearn'))"
```

看有没有你要改的 key。


## 五、安全事件:配置备份误提交(2026-11-26)

### 发生了什么

改 `target_hysteresis_pct` 时脚本自动备份了 `config.json.bak.<时间戳>`,
`git add -A` 把它一起提交并推到了 GitHub。

**文件里有明文凭证**:

```
broker.okx.api_key     6b31dde1...   ← 明文
broker.okx.api_secret  59E41D5F...   ← 明文
agent.token            dpapi1:...    ← DPAPI 加密(仅本机可解)
```

### 已做的处置

1. `git rm --cached` 该文件,加进 `.gitignore`(`config.json.bak.*`)
2. `git filter-branch` 清历史 → 没干净(留了 `refs/original/*`)
3. `git reset --soft` 回到泄露前的 commit,重新提交
4. `git push --force-with-lease` 覆盖远程
5. 删 `refs/original/*`、`git reflog expire --expire=now --all`、`git gc --prune=now`
6. 验证:`git log --all --name-only -- 'config.json.bak.*'` 无输出

### 还需要做的

**OKX 的 api_key / api_secret 必须轮换。**

理由:即使历史清干净了,GitHub 的 fork、缓存、第三方镜像可能已经抓过;
公开仓库的 commit 在删除前可能已被索引。

轮换步骤:
1. 登录 OKX → API 管理 → 删除旧 key
2. 新建 key(只开需要的权限:读 + 交易,不要开提现)
3. 在 Gray 设置页重新填(走 DPAPI 加密存 `cloud.key` 那种方式)
4. 确认 `config.json` 里不再有明文

### 以后怎么防

1. **备份文件永远不进库** —— `.gitignore` 已加 `config.json.bak.*`
2. **提交前看 `git status`** —— 这次是 `git add -A` 无脑加的
3. **凭证只在独立文件里** —— `cloud.key` 那种单独存 + DPAPI 加密,
   不塞 `config.json`
4. 建议加个 pre-commit 钩子扫明文密钥(形如 32 位 hex、`sk-`、`gk_`)


## 六、待实盘验证的修复(2026-11-26)

这些修复都通过了测试,但**还没经过真实交易验证** —— 因为实盘是早盘 9:30 跑的,
修复是当天中午才推的。下一交易日收盘后对一遍。

| 修复 | 怎么验证 | 期望 |
|---|---|---|
| **卖出不分批** | 看当日每只票卖出笔数 | 从 8~10 笔降到 1~2 笔 |
| 单票买入限次 3 | 看同一只票当日买入次数 | 不超过 3 |
| 碎单缓动 0.15 | 看同日同票买入间隔 | 不再出现 40 秒一笔 |
| 风控每日 30 笔 | 看当日总委托数 | 不超过 30 |
| 仓位自适应 | 看单票仓位 | 按近期表现 0.60~1.25 倍 |

### 怎么快速查

```bash
python -c "
import sys, time; sys.path.insert(0,'.')
from quantlab.data.store import store
start = time.time() - (time.time() % 86400)
rows = store().conn.execute(
    'select code, side, count(*) n from orders where ts>=? '
    'group by code, side order by n desc limit 10', (start,)).fetchall()
for r in rows: print(r['code'], r['side'], r['n'])
"
```

**2026-11-26 的基线**(修复前):

```
002579 sell 10      ← 目标:1~2
000530 sell 8       ← 目标:1~2
601138 buy 3        ← 已经对了(限次生效)
总计 45 笔          ← 目标:≤30
```

### 如果修了还是碎单

1. 确认主程序重启过 —— Python 进程不重启不会加载新代码
2. 查 `logs/maintain-*.log` 看跑的是哪个版本
3. 看 `sl_tranche` 表有没有残留状态(`TranchePlanner.clear()` 可清)

