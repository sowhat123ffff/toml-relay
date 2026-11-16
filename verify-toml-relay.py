"""Offline acceptance for the independent TOML relay.

Run: python verify-toml-relay.py
Uses a temporary cache directory, httpx.MockTransport for every upstream and
ASGITransport for the app. No network, Docker, .env or production data.
"""

import asyncio
import importlib.util
import os
from pathlib import Path
import tempfile

import httpx

ROOT = Path(__file__).resolve().parent
PASSED = 0
FAILED = 0

SHA = "a" * 64
GOOD = b"[0x82428E37]\nname = 'f'\nrva = '0x10'\n"
HTML = b"<!doctype html><html><body>blocked</body></html>"


def check(name, condition):
    global PASSED, FAILED
    if condition:
        PASSED += 1
    else:
        FAILED += 1
    print(f"{'PASS' if condition else 'FAIL'}: {name}")


def load_relay():
    spec = importlib.util.spec_from_file_location("isolated_toml_relay", ROOT / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Upstream:
    """Scripted upstream: maps URL substring -> response (or exception)."""

    def __init__(self):
        self.rules = []
        self.calls = []

    def set(self, *rules):
        self.rules = list(rules)
        self.calls = []

    def __call__(self, request):
        url = str(request.url)
        self.calls.append(url)
        for marker, reply in self.rules:
            if marker in url:
                if isinstance(reply, Exception):
                    raise reply
                return httpx.Response(reply[0], content=reply[1])
        return httpx.Response(404, content=b"404: Not Found")


OWN_RAW = "sowhat123ffff/steam-monitor/main/"
OWN_CDN = "cdn.jsdelivr.net/gh/sowhat123ffff"
AUTHOR_RAW = "https://raw.githubusercontent.com/huanyuejue"
GH_PROXY = "gh-proxy.com"


async def scenario(relay, root, upstream, coro):
    app = relay.create_app(root, transport=httpx.MockTransport(upstream))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://relay.test") as client:
            return await coro(client)


async def main():
    relay = load_relay()
    upstream = Upstream()
    path = f"/v2/pattern/steamclient/{SHA}.toml"

    with tempfile.TemporaryDirectory(prefix="toml-relay-") as temp:
        root = Path(temp)
        cache_file = root / "pattern/steamclient" / f"{SHA}.toml"

        # Control: without body validation, an HTML 200 really would be cached.
        original = relay.is_usable_toml
        relay.is_usable_toml = lambda body: bool(body)
        upstream.set((OWN_RAW, (200, HTML)))
        r = await scenario(relay, root, upstream, lambda c: c.get(path))
        check("control: validation disabled -> HTML 200 is cached and served",
              r.status_code == 200 and cache_file.read_bytes() == HTML)
        relay.is_usable_toml = original
        cache_file.unlink()

        # Owned Raw success, then cache hit with zero upstream calls.
        upstream.set((OWN_RAW, (200, GOOD)))
        r = await scenario(relay, root, upstream, lambda c: c.get(path))
        check("owned raw success returns TOML", r.status_code == 200 and r.content == GOOD)
        check("success headers: text/plain, immutable 1y, miss",
              r.headers["content-type"].startswith("text/plain")
              and r.headers["cache-control"] == "public, max-age=31536000, immutable"
              and r.headers["x-relay-cache"] == "miss")
        check("first source success stops the chain", len(upstream.calls) == 1)
        check("success is cached atomically (no leftover .tmp)",
              cache_file.read_bytes() == GOOD and not list(cache_file.parent.glob("*.tmp")))
        upstream.set()
        r = await scenario(relay, root, upstream, lambda c: c.get(path))
        check("second request is a cache hit without upstream calls",
              r.status_code == 200 and r.headers["x-relay-cache"] == "hit" and upstream.calls == [])
        cache_file.unlink()

        # Unusable 200 bodies are skipped, never cached, next source used.
        for label, body in (("HTML", HTML), ("empty", b""), ("malformed", b"[broken\nvalue =\n"),
                            ("comments-only", b"# nothing\n"), ("JSON", b'{"detail":"Not Found"}'),
                            ("plain-text error", b"404: Not Found")):
            upstream.set((OWN_RAW, (200, body)), (OWN_CDN, (200, GOOD)))
            r = await scenario(relay, root, upstream, lambda c: c.get(path))
            check(f"{label} 200 from owned raw is skipped and CDN is used",
                  r.status_code == 200 and r.content == GOOD and len(upstream.calls) == 2
                  and cache_file.read_bytes() == GOOD)
            cache_file.unlink()

        upstream.set((OWN_RAW, (200, b"\xef\xbb\xbf" + GOOD)))
        r = await scenario(relay, root, upstream, lambda c: c.get(path))
        check("UTF-8 BOM TOML is accepted", r.status_code == 200)
        cache_file.unlink()

        # Exact chain order, transport errors and 5xx fall through.
        upstream.set((OWN_RAW, httpx.ConnectError("down")), (OWN_CDN, (503, b"busy")),
                     (GH_PROXY, (200, GOOD)))
        r = await scenario(relay, root, upstream, lambda c: c.get(path))
        check("chain order: own raw -> own CDN -> author raw -> gh-proxy",
              r.status_code == 200 and [u.split("/")[2] for u in upstream.calls]
              == ["raw.githubusercontent.com", "cdn.jsdelivr.net", "raw.githubusercontent.com", "gh-proxy.com"]
              and AUTHOR_RAW in upstream.calls[2])
        cache_file.unlink()

        upstream.set((OWN_RAW, httpx.ReadTimeout("slow")), (OWN_CDN, (200, GOOD)))
        r = await scenario(relay, root, upstream, lambda c: c.get(path))
        check("read timeout falls through to next source", r.status_code == 200)
        cache_file.unlink()

        # Full miss: 404 + no-store, nothing cached.
        upstream.set()
        r = await scenario(relay, root, upstream, lambda c: c.get(path))
        check("all sources missing -> 404 no-store, 4 attempts, nothing cached",
              r.status_code == 404 and r.headers.get("cache-control") == "no-store"
              and len(upstream.calls) == 4 and not cache_file.exists())

        relay_budget = relay.TOTAL_BUDGET
        relay.TOTAL_BUDGET = 0
        upstream.set((OWN_RAW, (200, GOOD)))
        r = await scenario(relay, root, upstream, lambda c: c.get(path))
        check("exhausted budget answers 404 no-store without upstream calls",
              r.status_code == 404 and r.headers.get("cache-control") == "no-store" and upstream.calls == [])
        relay.TOTAL_BUDGET = relay_budget

        # Corrupted cache self-heals.
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(HTML)
        upstream.set((OWN_RAW, (200, GOOD)))
        r = await scenario(relay, root, upstream, lambda c: c.get(path))
        check("unusable cache file is dropped and refetched",
              r.status_code == 200 and r.headers["x-relay-cache"] == "miss"
              and cache_file.read_bytes() == GOOD)
        cache_file.unlink()

        # Cache write failure still serves the body.
        blocker = root / "ipc"
        blocker.write_bytes(b"not a directory")
        upstream.set((OWN_RAW, (200, GOOD)))
        r = await scenario(relay, root, upstream,
                           lambda c: c.get(f"/v2/ipc/steamclient/{SHA}.toml"))
        check("cache write failure still serves valid TOML", r.status_code == 200 and r.content == GOOD)
        blocker.unlink()

        # Request validation and surface area.
        upstream.set((OWN_RAW, (200, GOOD)))
        bad_paths = [f"/v2/lua/steamclient/{SHA}.toml", f"/v2/pattern/steam/{SHA}.toml",
                     f"/v2/pattern/steamclient/{SHA.upper()}.toml", "/v2/pattern/steamclient/abc.toml",
                     f"/v2/pattern/steamclient/..%2F{SHA[3:]}.toml"]
        results = await scenario(relay, root, upstream,
                                 lambda c: asyncio.gather(*(c.get(p) for p in bad_paths)))
        check("invalid channel/component/sha -> 404 no-store, no upstream calls",
              all(x.status_code == 404 and x.headers.get("cache-control") == "no-store" for x in results)
              and upstream.calls == [])

        extra = await scenario(relay, root, upstream, lambda c: asyncio.gather(
            c.get("/health"), c.get("/docs"), c.get("/openapi.json"),
            c.get(f"/toml/v2/pattern/steamclient/{SHA}.toml")))
        check("health endpoint ok", extra[0].status_code == 200 and extra[0].json() == {"status": "ok"})
        check("docs/openapi disabled", extra[1].status_code == 404 and extra[2].status_code == 404)
        check("CloudActivate-style /toml/v2/ path is not served", extra[3].status_code == 404)
        check("no files written outside the cache root",
              {p.name for p in root.iterdir()} <= {"pattern", "ipc"})

    # Deployment files.
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    check("container runs as non-root", "USER relay" in dockerfile)
    check("compose publishes only on host loopback",
          '"127.0.0.1:${TOML_RELAY_PORT:-8013}:8080"' in compose and "0.0.0.0" not in compose)
    sources = [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts
               and p.suffix in {".py", ".yml", ".txt", ""} and p.name != Path(__file__).name]
    check("project code has no CloudActivate dependency",
          not any(token in p.read_text(encoding="utf-8", errors="ignore")
                  for p in sources for token in ("cloudact.", "cloud-activate", "cloud_activate")))


asyncio.run(main())
print(f"RESULT: {PASSED} passed, {FAILED} failed")
raise SystemExit(1 if FAILED else 0)
