import asyncio
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_client: Optional[httpx.AsyncClient] = None


def get_async_client() -> httpx.AsyncClient:
    """Process-wide shared httpx client.

    Timeouts (relevant for streaming chat):
      * ``connect=10s`` — fast-fail on DNS/TLS hiccups
      * ``read=30s``    — how long we wait between bytes from the
        upstream. The previous 120s meant a broken/slow upstream
        (e.g. Aliyun cold-start, or a model that's not actually
        served by the vendor) kept the user staring at a "loading…"
        chat bubble for two minutes before we'd even consider it a
        failure. 30s is enough for legitimate cold starts without
        letting a clearly-broken upstream block the UI.
      * ``write=30s``   — body upload ceiling
      * ``pool=10s``    — how long we wait for a free connection
        from the pool when all keep-alives are busy
    """
    global _client
    if _client is None or _client.is_closed:
        timeout = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
        limits = httpx.Limits(
            max_connections=50, max_keepalive_connections=20, keepalive_expiry=60.0
        )
        _client = httpx.AsyncClient(timeout=timeout, limits=limits)
    return _client


async def aclose_async_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def post_with_retry(
    url: str,
    *,
    json: dict,
    headers: dict,
    retries: int = 2,
    backoff_base: float = 0.4,
) -> httpx.Response:
    client = get_async_client()
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return await client.post(url, json=json, headers=headers)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            last_exc = e
            if attempt >= retries:
                raise
            await asyncio.sleep(backoff_base * (2**attempt))
    raise last_exc if last_exc else RuntimeError("request_failed")
