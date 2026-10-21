from __future__ import annotations

import httpx
import pytest

from adreel import net


def _client(responses):
    seq = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        item = next(seq)
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_retries_5xx_then_returns_ok():
    client = _client([httpx.Response(503), httpx.Response(200, json={"ok": True})])
    r = await net.request("GET", "https://x.test", who="t", client=client, pauses=(0, 0))
    assert r.status_code == 200


async def test_4xx_returned_immediately_without_retry():
    client = _client([httpx.Response(401)])  # второй ответ не нужен: повтора быть не должно
    r = await net.request("GET", "https://x.test", who="t", client=client, pauses=(0, 0))
    assert r.status_code == 401


async def test_429_not_retried_when_disabled():
    client = _client([httpx.Response(429)])
    r = await net.request("GET", "https://x.test", who="t", client=client, pauses=(0,),
                          retry_429=False)
    assert r.status_code == 429


async def test_network_errors_exhaust_to_service_unavailable():
    err = httpx.ConnectError("boom")
    client = _client([err, err, err])
    with pytest.raises(net.ServiceUnavailable, match="ConnectError"):
        await net.request("GET", "https://x.test", who="t", client=client, pauses=(0, 0))
