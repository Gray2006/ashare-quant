这一版是**清理版**：把公开仓库里剩下的个人版残留清掉，并修一个会崩的页面。

## 修：因子挖掘页的「加密」标签会崩

因子挖掘页原来有「A股 / 加密」两个标签，但公开版并没有随包分发 `quantlab/crypto`
（README 也写明不含加密货币）。点到「加密」标签、或者点「全部重跑」，会直接
`ModuleNotFoundError: No module named 'quantlab.crypto'`。

本版去掉了加密标签，只保留 A 股。

## 改：本机控制接口默认关闭

`agent`（本地 HTTP 接口，给脚本/AI 代理操作模拟盘用）原来默认是开的，但设置页没有
开关、README 也没写——等于一启动就默默开了个本地监听端口。

本版改为**默认关闭**。它本身是安全的（只绑 `127.0.0.1`、强制 `X-Token`、
校验 Host/Origin 防 DNS rebinding、token 32 字节随机并加密落盘），只是不该
默认替用户开。要用的把 `config.json` 里 `agent.enabled` 改成 `true` 重启，
README 新增了「本机控制接口」一节。

## 删：个人部署文档与 OKX 残留

- 移出 `docs/ops.md`：那是作者自己的服务器运维手册，写着计划任务、真实持仓、
  以及一次凭证误提交的事故记录。对使用者没有价值，也不该公开。
- 删除 `quantlab/security/okx_creds.py`、`tools/migrate_okx_keys.py`、
  `tests/test_okx_creds.py`：`broker/okx.py` 在 v1.0.15 已删，这三件成了
  互相引用的孤岛。`config.py` 的 `SECRET_PATHS` 同步去掉 okx 三项。
- 删除配置里从没被读过、也没人能用的死键：`crypto_quant`、`watchlist_crypto`、
  `refresh_seconds.crypto`。
- 交易页去掉一个过滤 `gateway != "okx"` 的无意义残留。

## 修：测试隔离

`pytest tests` 原来在收集阶段就报错——几个测试模块都在 import 期设 `GRAY_HOME`，
但 `quantlab.config` 一个进程只 import 一次，后跑的模块会用到前一个模块的临时目录，
断言对不上。现在不依赖环境变量，直接用模块自己的路径。

## 验证

- `pytest tests`：144 passed
- `compileall`：0 错误
- 离屏建页：27/27
- `tools/check_config_defaults.py`：无默认值不一致
- `tools/scan_secrets.py`：242 个已跟踪文件，0 命中

## 边界（不变）

模拟盘与自动交易都只在本地撮合，**不接券商、不同花顺、不发真单**；
不含加密货币；数据回流默认关闭；模拟盈亏是练习数据，不构成投资建议。

## 安装

1. 下载 ZIP，解压到一个普通目录（别放 `C:\Program Files`）
2. 双击 `install.bat`，等 1~3 分钟
3. 桌面出现「量化工作台」

或者最省事：下载
<https://raw.githubusercontent.com/Gray2006/ashare-quant/main/install-online.bat>
双击，自动拉最新版装到 `%USERPROFILE%\ashare-quant`。

已经是旧版本的话，设置页点「检查更新 → 下载并更新」即可，
`config.json`、`data/`（含模拟盘账本）、`.venv/` 都会保留。

MIT 许可。
