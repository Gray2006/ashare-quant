第一个桌面版。之前只有 CLI / Web 版，现在是 Windows 上双击就能开的 PySide6 程序，22 个页面。

## 修复

- **双击「量化工作台」没反应**：`start.bat` / `install.bat` 把 `.venv\Scripts\python.exe`
  又拼了一次 `w.exe`，得到不存在的 `python.exew.exe`，`start` 找不到文件就静默失败。
  现在虚拟环境存在与否都能拼对。
- **公司分析页点「评估报告」不跳转**：`CompanyPage.open_eval` 信号写好了、按钮也
  emit 了，但主窗口的 `_wire_page` 里漏了 `.connect`，点了没反应。已接通，会带着
  股票代码跳到公司评估页并直接开始生成。
- **评估报告的导出按钮可能一直是灰的**：任务线程收尾时只恢复了「生成」按钮，
  现在按手里是否还有报告统一恢复「保存 / 复制 / 导出 PDF」。

## 新增

- **意见反馈**（设置页）：写内容、可选联系方式，然后保存到本地
  `feedback/notes.jsonl`，或一键复制成 GitHub Issue 正文。默认不联网、不上传；
  勾选附带环境信息也只带版本号、系统版本、Python 与 Qt 版本，不含自选股、
  持仓、路径。
- **更新**（设置页）：把 GitHub 仓库链接粘进去就能检查新版本。支持
  `https://github.com/用户名/仓库名`、带 `.git`、带 `/tree/main`、
  `git@github.com:...` 和 `用户名/仓库名` 简写。源码运行时点「下载并更新」会
  下载、解压，程序退出后由脚本覆盖程序文件并自动重启；覆盖时排除
  `.venv`、`.git`、`data`、`memory`、`config.json`、`*.db`，配置和数据不会被冲掉。
  打包版不做自替换，会提示去发布页手动下载。
- GitHub API 的请求先走 requests，失败自动退到系统 curl —— 部分网络下
  requests 的证书链会被本地代理挡住，而 curl 走 Windows 证书存储能通。

## 安装

1. 下载 ZIP，解压到一个普通目录（别放 `C:\Program Files`，那里没写权限）
2. 双击 `install.bat`，等 1~3 分钟
3. 桌面出现「量化工作台」，双击就能用

要求 Windows 10/11、已装 Python 3.10+（安装时勾选 "Add python.exe to PATH"）。
不需要管理员权限；`config.json` 和 `data/` 都在解压目录里，卸载或换机直接拷走。

## 边界

只做 A 股公开行情、技术分析、条件选股与历史回测。不接券商、不同花顺、不下单、
不自动交易；不含加密货币；回测结果只代表历史，不构成投资建议。

MIT 许可。
