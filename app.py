"""OpenSteamTool metadata TOML relay, independent from CloudActivate.

GET /v2/{channel}/{component}/{sha256}.toml
    {sha256} is the SHA-256 of the target Steam DLL, not of the TOML body.
    Upstream chain: owned fork Raw -> owned fork jsDelivr -> author Raw -> gh-proxy.

Cache semantics:
  - Only bodies that parse as a non-empty TOML table are cached or served.
    HTML/JSON/plain-text error pages from any upstream are skipped.
  - Content is addressed by DLL SHA-256, but upstream sometimes adds entries to
    an existing file (2026-09-13: two pattern/steamclient files, +5 lines each),
    and the DLL refetches when a function is missing. So a cached file older
    than REFRESH_AFTER is fetched again; if no source has it, the cached copy
    is still served. Cache-Control matches that window.
  - Misses always answer 404 + no-store so the DLL moves straight to its own
    direct fallbacks (it never retries a 404) and edges never pin a miss.
"""

import asyncio
import logging
import os
import re
import tempfile
import time
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Response

log = logging.getLogger("toml_relay")

CHANNELS = {"pattern", "ipc"}
COMPONENTS = {"steamclient", "steamui"}
SHA_RE = re.compile(r"^[0-9a-f]{64}$")

UPSTREAMS = [
    "https://raw.githubusercontent.com/sowhat123ffff/steam-monitor/main/{channel}/{component}/{sha256}.toml",
    "https://cdn.jsdelivr.net/gh/sowhat123ffff/steam-monitor@main/{channel}/{component}/{sha256}.toml",
    "https://raw.githubusercontent.com/huanyuejue/steam-monitor/main/{channel}/{component}/{sha256}.toml",
    "https://gh-proxy.com/https://raw.githubusercontent.com/huanyuejue/steam-monitor/main/{channel}/{component}/{sha256}.toml",
]

# The DLL waits up to 10 s per receive; finishing the whole chain within 8 s
# lets a miss reach the DLL as a fast 404 instead of a timeout plus retry.
PER_SOURCE_TIMEOUT = 4.0
TOTAL_BUDGET = 8.0

# A cached file is served without asking upstream for this long.
REFRESH_AFTER = 24 * 3600
CACHE_CONTROL_200 = f"public, max-age={REFRESH_AFTER}"


def is_usable_toml(body: bytes) -> bool:
    try:
        return bool(tomllib.loads(body.decode("utf-8-sig")))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return False


def _write_atomic(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _toml_response(body: bytes, cache_state: str) -> Response:
    return Response(content=body, media_type="text/plain; charset=utf-8",
                    headers={"Cache-Control": CACHE_CONTROL_200,
                             "X-Relay-Cache": cache_state})


def create_app(cache_dir: Path, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(transport=transport, follow_redirects=True,
                                     headers={"User-Agent": "opensteamtool-toml-relay/1.0"}) as client:
            app.state.client = client
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def no_store_unless_ok(request, call_next):
        # Misses and unmatched paths must never be pinned by an edge cache.
        response = await call_next(request)
        if response.status_code != 200:
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/v2/{channel}/{component}/{sha256}.toml")
    async def fetch(channel: str, component: str, sha256: str) -> Response:
        if channel not in CHANNELS or component not in COMPONENTS or not SHA_RE.match(sha256):
            raise HTTPException(status_code=404)

        cache_path = cache_dir / channel / component / f"{sha256}.toml"
        stale = None
        try:
            cached = cache_path.read_bytes()
            age = time.time() - cache_path.stat().st_mtime
        except FileNotFoundError:
            cached = None
        if cached is not None:
            if is_usable_toml(cached):
                if age < REFRESH_AFTER:
                    return _toml_response(cached, "hit")
                stale = cached
            else:
                log.warning("dropping unusable cache file %s", cache_path)
                cache_path.unlink(missing_ok=True)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + TOTAL_BUDGET
        last_err = "budget exhausted"
        for template in UPSTREAMS:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            url = template.format(channel=channel, component=component, sha256=sha256)
            try:
                r = await app.state.client.get(url, timeout=min(PER_SOURCE_TIMEOUT, remaining))
            except httpx.HTTPError as e:
                last_err = f"{url} -> {type(e).__name__}"
                log.info("upstream failed: %s", last_err)
                continue
            if r.status_code == 200 and is_usable_toml(r.content):
                try:
                    _write_atomic(cache_path, r.content)
                except OSError as e:
                    log.warning("could not cache %s: %s", cache_path, e)
                return _toml_response(r.content, "miss" if stale is None else "refreshed")
            last_err = f"{url} -> HTTP {r.status_code}" + (" (not TOML)" if r.status_code == 200 else "")
            log.info("upstream unusable: %s", last_err)

        if stale is not None:
            # Keep serving it, and wait another REFRESH_AFTER before asking upstream again.
            log.info("refresh failed, serving cached %s (%s)", cache_path, last_err)
            try:
                os.utime(cache_path)
            except OSError:
                pass
            return _toml_response(stale, "stale")

        raise HTTPException(status_code=404, detail=f"all sources failed ({last_err})")

    return app


logging.basicConfig(level=logging.INFO)
app = create_app(Path(os.environ.get("TOML_RELAY_CACHE_DIR", "data/cache")))
