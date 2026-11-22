# 安全说明

- 本程序不接收网络入站连接，不开放本地 HTTP 控制端口。
- 不读取、不保存任何券商账号、API key、密码或令牌。
- 行情请求走公开 HTTPS 接口，带明确的 User-Agent，不伪造来源。
- 依赖只有 PySide6、pyqtgraph、numpy、pandas、requests。
- 发现安全问题请开 issue，说明版本、复现步骤与影响；不要公开可直接利用的细节。
