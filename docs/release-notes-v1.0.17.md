测试数据里有一个不该出现的字符串。

`tests/test_secret_scan.py` 里用来验证「凭证扫描能命中」的样例，前 8 位取自一个真实
密钥。虽然剩下 24 位是编的、还原不出完整密钥，但真实前缀不该出现在公开仓库里。

本版把样例换成完全无关的十六进制串，功能不变（9 项测试全过）。

如果你之前 clone 过本仓库，确认本地文件已更新即可；没有其他影响。

## 验证

- `pytest tests/test_secret_scan.py`：9 passed
- `tools/scan_secrets.py`：242 个已跟踪文件，0 命中
- `compileall`：0 错误

MIT 许可。
