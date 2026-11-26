这一版是 **v1.0.14 的隐私修正**。如果你装了 1.0.14，建议尽快更新。

## 重要：默认值回滚

v1.0.14 里有两处配置默认值被写成了「开」，与 PRIVACY.md 的承诺相反：

| 项 | v1.0.14 | v1.0.15 |
|---|---|---|
| `cloud.enabled` | `true` | `false` |
| `feedback.enabled` | `true` | `false` |

原因是一次提交把个人版的 `config.py` 整体覆盖到了公开版，连带把这两个开关翻成了开。
配合仓库里的 `cloud_defaults.json`（内含作者维护的接收端地址与公共 key），
新装用户一启动就同时满足「开关开 + 地址有 + key 有」三个条件，具备上传能力。

本版已回滚为默认关闭，并且 `_apply_cloud_defaults()` 现在**只填地址、不替你开开关**：
装好后仍然是关的，要不要开、开给谁，由你在设置页自己决定。

**已经装了 1.0.14 的**：更新到本版后，如果你之前没手动开过，`config.json` 里的
`feedback.enabled` 可能已经是 `true`（因为旧默认值被写进了配置文件）。
请到「设置」页确认数据回流是关的；或者直接删掉 `config.json` 让新默认值生效
（会丢自选股，模拟盘账本在 `data/` 里，不受影响）。

## 清理

- 删除两个误入公开仓库的个人版页面（`live_page.py` / `live_learn_page.py`），
  它们写死了作者本机的钱包路径，且没有挂在导航里，从界面根本进不去。
- 删除 `quantlab/broker/okx.py`：它依赖的 `quantlab.data.okx` 没有随包分发，
  导入即报错；README 也明确说不含加密货币。
- 自动交易页的通道下拉去掉「同花顺(真钱)」：公开版没有 `broker/ths.py`，
  选中就会 `ModuleNotFoundError` 崩掉。现在只保留「模拟盘」。
- `tools/scan_secrets.py`、`tools/install_secret_hook.py` 去掉硬编码的本机路径，
  改为环境变量按需追加。
- `config.py` 移除 `broker.ths` / `broker.okx` 段落。

## 验证

- `compileall` 全绿
- 离屏启动建页 27/27
- 自动交易页通道仅「模拟盘」
- 模拟新装用户首次启动（带 `cloud_defaults.json`）：`cloud.enabled()` 与
  `feedback.enabled()` 均为 `False`

## 边界（不变）

模拟盘与自动交易都只在本地撮合，**不接券商、不同花顺、不发真单**；
不含加密货币；模拟盈亏是练习数据，不构成投资建议。

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
