"""Повтор сетевых вызовов к внешним сервисам — в одном месте на весь проект.

Перенесено из AI_Host (videonews/net.py), переписано на async.

ЗАЧЕМ. Конвейер идёт стадиями, и каждая следующая стоит денег. Секундная сетевая
яма посреди прогона не должна выбрасывать уже оплаченную работу.

ЧТО ПОВТОРЯЕМ. Только то, что может пройти со второй попытки: обрыв связи, таймаут,
5xx и по желанию вызывающего 429. Ошибки смысла (401 ключ, 402 баланс, 400 кривой
запрос) возвращаем сразу: повтор их не вылечит, а время съест.

Claude и fal сюда не заведены: их SDK повторяют запросы сами.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger("adreel.net")

#: сбои, на которых имеет смысл просто повторить
RETRY_EXC = (
    httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
    httpx.WriteTimeout, httpx.PoolTimeout, httpx.RemoteProtocolError,
)
#: паузы МЕЖДУ попытками; их количество задаёт и число повторов (попыток = len + 1)
PAUSES: tuple[float, ...] = (3, 10, 30)


class ServiceUnavailable(RuntimeError):
    """Сервис не ответил после всех повторов."""


class TooLarge(RuntimeError):
    """Ответ больше max_bytes — дочитывать не стали."""


#: тело уже распаковано при чтении потоком — эти заголовки врали бы про новый ответ
_STREAM_HEADERS = ("content-encoding", "content-length", "transfer-encoding")


async def _send(http: httpx.AsyncClient, method: str, url: str, max_bytes: int | None,
                who: str, **kw: Any) -> httpx.Response:
    if max_bytes is None:
        return await http.request(method, url, **kw)
    # Читаем потоком и обрываем на лимите: чужой сервер может отдать гигабайт, а r.content
    # сначала прочитал бы его в память целиком и только потом дал проверить размер.
    async with http.stream(method, url, **kw) as r:
        body = bytearray()
        async for chunk in r.aiter_bytes():
            body += chunk
            if len(body) > max_bytes:
                raise TooLarge(f"{who}: файл больше {max_bytes // 1024 // 1024} МБ")
    headers = [(k, v) for k, v in r.headers.multi_items() if k.lower() not in _STREAM_HEADERS]
    return httpx.Response(r.status_code, headers=headers, content=bytes(body),
                          request=r.request)


async def request(
    method: str,
    url: str,
    *,
    who: str = "сервис",
    timeout: float = 600,
    retry_429: bool = True,
    client: httpx.AsyncClient | None = None,
    pauses: tuple[float, ...] = PAUSES,
    max_bytes: int | None = None,
    **kw: Any,
) -> httpx.Response:
    """HTTP-запрос с повтором. Возвращает ответ, в т.ч. с 4xx — их разбирает вызывающий.

    who — имя сервиса для лога и текста ошибки: по нему сразу видно, кто отвалился.
    retry_429 — повторять ли «слишком часто». Для ElevenLabs 429 означает исчерпанную
    квоту, повтор бессмыслен, поэтому там передают False.
    client — для тестов (MockTransport); без него создаётся одноразовый клиент.
    max_bytes — для чужих адресов: больше — TooLarge, не дочитывая ответ.
    """
    last = "?"
    own = client is None
    http = client or httpx.AsyncClient(timeout=timeout)
    try:
        for pause in (*pauses, None):
            try:
                r = await _send(http, method, url, max_bytes, who, **kw)
            except RETRY_EXC as exc:
                last = f"{type(exc).__name__}: {str(exc)[:120]}"
            else:
                if r.status_code < 500 and not (r.status_code == 429 and retry_429):
                    return r
                last = f"HTTP {r.status_code}: {r.text[:120]}"
            if pause is None:
                break
            log.warning("%s не ответил (%s) — повтор через %s с", who, last, pause)
            await asyncio.sleep(pause)
    finally:
        if own:
            await http.aclose()
    raise ServiceUnavailable(f"{who} недоступен после {len(pauses) + 1} попыток. Последнее: {last}")


async def download(url: str, *, who: str, timeout: float = 600, **kw: Any) -> bytes:
    """Скачать файл целиком. Результат генерации к этому моменту уже оплачен —
    поэтому качаем с повторами, а не одной попыткой."""
    r = await request("GET", url, who=who, timeout=timeout, follow_redirects=True, **kw)
    r.raise_for_status()
    return r.content
