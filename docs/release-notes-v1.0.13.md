这一版加了「一键在线安装」：只需要下载一个 bat，双击就装好。

## 新增

- **`install-online.bat`**：双击后自动完成
  1. 检查电脑里有 Python 3.10+（没有就提示去哪装）
  2. 查 GitHub 最新 release
  3. 下载源码 zip 到临时目录
  4. 解压到 `%USERPROFILE%\ashare-quant`
  5. 调 `install.bat` 建虚拟环境、装依赖、生成桌面快捷方式
- 已装过再运行会提示「已经装过」，不会覆盖 `data/` 里的账本；
  升级仍走程序内「设置 → 检查更新」。
- `scripts/install_online.ps1` 是实际逻辑，支持 `-Target` 指定目录、
  `-SkipInstall` 只下代码不装依赖（测试用）。

## 用法

在浏览器打开
<https://raw.githubusercontent.com/Gray2006/ashare-quant/main/install-online.bat>，
右键另存为，双击。

## 验证

- 用临时目录跑 `-SkipInstall`：查到 v1.0.12、下载、解压、`run.py` 就位，退出码 0。
- 脚本存为 UTF-8 with BOM，PowerShell 5.1 下中文不乱码。

## 边界

模拟盘与自动交易都只在本地撮合，**不接券商、不同花顺、不发真单**；
不含加密货币；模拟盈亏是练习数据，不构成投资建议。

## 安装

已装过 1.0.12 的话，设置页点「检查更新 → 下载并更新」即可，
`config.json`、`data/`（含模拟盘账本）、`.venv/` 都会保留。

MIT 许可。
