"""Память о запущенных генерациях: чтобы не платить дважды за одно и то же.

Урок из AI_Host (render/heygen.py): таймаут выбрасывал уже оплаченный рендер,
а повторная попытка запускала его с нуля и платила ещё раз — 14.08.2026 так
сгорело три ролика из четырёх.

Поэтому request_id пишется на диск СРАЗУ после отправки. При повторе сначала
спрашиваем fal про старый запрос: готов — забираем бесплатно, ещё идёт — ждём его же.

КЛЮЧ — отпечаток ВХОДА (модель + хеши картинок + промпт + параметры), а не URL
загруженных файлов: одна и та же картинка при каждой загрузке получает новый URL.
Изменился вход — другой ключ, честная новая генерация.

На этапе 1 переедет в таблицу generations; интерфейс останется тем же.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("adreel.gen.pending")


def fingerprint(model_id: str, **parts: Any) -> str:
    """Стабильный ключ входа. bytes хешируются, остальное сериализуется в JSON."""
    norm = {
        k: hashlib.sha256(v).hexdigest() if isinstance(v, bytes) else v
        for k, v in sorted(parts.items())
    }
    raw = json.dumps({"model": model_id, **norm}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class PendingStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    def _read(self) -> dict[str, dict[str, str]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, dict[str, str]]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            # Не смогли запомнить — в худшем случае повтор сгенерирует заново.
            # Ронять из-за этого уже оплаченную генерацию нельзя.
            log.warning("не смог запомнить генерацию (%s)", exc)

    async def get(self, key: str) -> dict[str, str] | None:
        async with self._lock:
            return self._read().get(key)

    async def remember(self, key: str, model_id: str, request_id: str) -> None:
        async with self._lock:
            data = self._read()
            data[key] = {"model": model_id, "request_id": request_id}
            self._write(data)

    async def forget(self, key: str) -> None:
        async with self._lock:
            data = self._read()
            if data.pop(key, None) is not None:
                self._write(data)
