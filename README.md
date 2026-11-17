# toml-relay

OpenSteamTool 元数据 TOML 中转缓存。独立项目，与 CloudActivate 无代码、容器、数据卷或域名耦合。

## 契约

```text
GET https://toml.nothingaming.com/v2/{channel}/{component}/{sha256}.toml
```

- `channel` ∈ `pattern`、`ipc`；`component` ∈ `steamclient`、`steamui`；`sha256` 为 64 位小写十六进制。
- `{sha256}` 是目标 Steam DLL 的 SHA-256，不是 TOML 正文的 SHA-256。
- 200：`text/plain; charset=utf-8`，`Cache-Control: public, max-age=86400`，`X-Relay-Cache: hit|miss|refreshed|stale`。
- 其它一切（未发布、参数非法、未知路径）：404 + `Cache-Control: no-store`。DLL 不重试 404，直接进入自己的直连回退。
- `GET /health` → `{"status":"ok"}`。

## 回源与缓存

回源顺序：自有 `sowhat123ffff/steam-monitor` main Raw → 自有 jsDelivr → 作者 Raw → gh-proxy。

- 只有能解析为非空 TOML 表的正文才缓存和返回；任何上游返回的 HTML/JSON/纯文本错误页都跳过。
- 单源超时 4 秒，整条链 8 秒预算，低于 DLL 10 秒接收超时，未命中时 DLL 收到快速 404 而不是超时再重试。
- 缓存写入：同目录临时文件 + `os.replace`，不会留下截断文件；读取时再次校验，坏文件自动删除并重新回源。
- 缓存超过 1 天（`REFRESH_AFTER`）就重新回源：上游会给同一个 SHA 的文件补条目（2026-09-13 有两个 `pattern/steamclient` 文件各加了 5 行），DLL 缺函数时会删掉本地缓存重下，中转不能一直给旧文件。回源拿到可用 TOML → 更新缓存，`refreshed`；全部失败 / 404 / 不是 TOML → 照样回旧缓存 200，`stale`，并再等 1 天才重试。`hit` 只表示 1 天内的缓存。
- 缓存位于容器 `/data/cache`（命名卷 `toml-relay_toml-cache`），与任何其它项目隔离。

## 本地验证

```bash
python verify-toml-relay.py
```

离线：临时目录、`httpx.MockTransport`、ASGI，无网络/Docker/生产数据。含对照组（关闭正文校验时 HTML 200 确实会被缓存）。

## 部署（宝塔 + Cloudflare，用户执行）

1. Cloudflare：新增 `toml` 子域名记录（橙云代理）指向服务器。
2. 服务器：克隆本项目到独立目录（例如 `/www/wwwroot/toml-relay`），先确认宿主端口 `8013` 未被占用（被占用则在 `.env` 写 `TOML_RELAY_PORT=<端口>`）。
3. `docker compose up -d --build`；本机自测：

   ```bash
   curl -fsS -D- -o /tmp/relay.toml http://127.0.0.1:8013/v2/pattern/steamclient/669fa08e259277fbf5a1dfe15ce88afe734e89151194304b44d92f418dcc985b.toml
   sha256sum /tmp/relay.toml   # 期望 7e471686ac7f0bf02f9e6b68187ba33987236a66bcb550efa747a2ab206990ec
   ```

4. 宝塔：新建站点 `toml.nothingaming.com`，整站反向代理到 `http://127.0.0.1:8013`，配置 HTTPS（Cloudflare Full (strict) 需要源站证书）。
5. 公网复查同一 URL：状态 200、`text/plain`、正文 SHA-256 同上、`X-Relay-Cache` 第二次为 `hit`；未发布 SHA 返回 404 `no-store`。

## 回滚

`docker compose down`（保留命名卷）并停用宝塔站点。DLL 无需改配置：中转不可用时自动直连自有 Raw/CDN 与作者来源。
