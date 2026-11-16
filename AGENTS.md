# toml-relay 规则

回复使用中文。先读本文与 README.md。

- 独立项目：不导入、不引用、不部署到 CloudActivate（代码、compose、数据卷、域名均分离）。DLL 只依赖 README 中的 URL 契约。
- 改动契约（路径、状态码语义、缓存头）等于改 DLL 行为，需同步 `OpenSteamTool/src/Utils/SteamMetadata/RemoteToml.cpp` 与其隔离测试。
- 每次改动在 `CHANGELOG/` 新建 `YYYY-MM-DD-<主题>.md`：变更 + 实际验证数字。
- `verify-toml-relay.py` 必须保持全部通过，新功能补用例并带对照组。只用临时目录与 MockTransport，不接触生产。
- 不入库 `.env`、`data/`、日志或任何凭据；本项目不需要任何密钥。
- 不自动创建远程仓库或推送；生产部署由用户在宝塔终端执行，生产目录不手改文件。
