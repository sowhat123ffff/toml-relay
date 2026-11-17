# 2026-11-17 缓存超过 1 天重新回源

上游有时会给已有的 SHA 文件补条目（2026-09-13：作者仓库两个 `pattern/steamclient` 文件各 +5 行）。DLL 为这种情况做了自愈：签名里找不到某个函数时，删掉本地缓存重新下载一次（`PatternLoader.cpp` `FindPattern`）。但中转是 DLL 的第一个来源，原来缓存命中后永远不再回源，还给 1 年 `immutable` 缓存头，DLL 重下拿到的还是旧文件，第一个可用回答就停，永远到不了已经补全的 GitHub。依赖新函数的 hook 会一直停用，直到 Steam 更新换了 SHA。

## 变更

- `app.py`：缓存超过 `REFRESH_AFTER`（24 小时）就重新走四个上游。拿到可用 TOML → 覆盖缓存，`X-Relay-Cache: refreshed`；全部失败 / 404 / 返回的不是 TOML → 照样回旧缓存 200（`stale`），并把缓存时间刷新，再等 24 小时才重试，上游挂了不会每个请求都多等 8 秒。1 天内的缓存仍是 `hit`、不回源。`Cache-Control` 从 `public, max-age=31536000, immutable` 改为 `public, max-age=86400`，和刷新周期一致，前面的 CDN 不会把旧文件钉一年。
- `verify-toml-relay.py`：新增 9 个用例（含对照组），缓存头断言改为 1 天。
- `README.md`：契约里的缓存头与 `X-Relay-Cache` 取值；「回源与缓存」写明刷新规则。

对 DLL 的影响：DLL 只认「200 + 能解析的 TOML」和 404，不读 `Cache-Control` / `X-Relay-Cache`（全仓搜索 `OpenSteamTool/src` 无引用）。状态码语义不变，刷新失败也仍是 200，所以 `RemoteToml.cpp` 不需要改。v7 签名修复同样只看状态码和正文。

## 验证

| 检查 | 结果 |
|---|---|
| `verify-toml-relay.py`（改前） | 27 passed / 0 failed |
| `verify-toml-relay.py`（改后） | 36 passed / 0 failed |
| 对照组：刷新周期设成无限（等于原来的行为），缓存已有 1 天多、上游已更新 | 仍回旧文件 `hit`、0 次回源 |
| 新用例 | 1 天内 → `hit`、0 次回源；超过 1 天且上游已补条目 → 回新文件 `refreshed`、1 次回源、缓存被覆盖；上游全 404 / 全断 / 全回 HTML → 回旧缓存 200 `stale`、4 次回源、缓存不变，紧接着下一次是 `hit`、0 次回源 |
| 变异检查：把「是否超过 1 天」恒设为未超过 | 32 / 4，刷新与 `stale` 的 4 个用例失败；恢复后 36 / 0 |
| 真实上游（本机 ASGI、临时缓存目录） | `pattern/steamclient/669fa08e…` 第一次 200 `miss`、第二次 200 `hit`、把缓存改成 25 小时前后 200 `refreshed`；三次正文 SHA-256 都是 `7e471686ac7f…`，与本机 Steam 的 `metadata-v2` 缓存一致 |
| 改前用中转真实代码复现 | 上游补条目后，中转回 200 `hit`、0 次回源、不含新条目；删掉中转缓存后才拿到新文件 |
| 生产 | 未部署。需要在宝塔终端重建容器生效；已有的缓存文件按修改时间算，超过 1 天的会在下一次请求时回源 |
