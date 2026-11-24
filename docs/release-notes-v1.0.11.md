这一版修了因子挖掘页「一键挖掘」直接失败的问题。

## 修复

- **一键挖掘报 `No module named 'scipy'`**：分层单调性那一步用了
  `pandas.Series.corr(method="spearman")`，pandas 内部会去 import scipy，
  而安装包不带 scipy，于是整轮挖掘在评估第一个候选时抛异常，界面显示「失败」。
  改成先 `rank()` 再算 Pearson（数学上等价，不需要 scipy），和其他模块
  （`factor_analysis`、`riskband`、`dsl`）的处理方式一致。

## 验证

- 真实数据复现：修复前抛 `ModuleNotFoundError: scipy`；修复后返回 63 行候选。
- 界面离屏跑一次：120 日 × 3 轮 × 200 只，91 个候选，27.1 秒完成，
  按钮恢复可用，不再提示失败。

## 边界

模拟盘与自动交易都只在本地撮合，**不接券商、不同花顺、不发真单**；
不含加密货币；模拟盈亏是练习数据，不构成投资建议。

## 安装

1. 下载 ZIP，解压到一个普通目录（别放 `C:\Program Files`）
2. 双击 `install.bat`，等 1~3 分钟
3. 桌面出现「量化工作台」

已经是 1.0.10 的话，设置页点「检查更新 → 下载并更新」即可，
`config.json`、`data/`（含模拟盘账本）、`.venv/` 都会保留。

MIT 许可。
