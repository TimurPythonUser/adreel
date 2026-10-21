"""Состояние диалога НА ДИСКЕ, а не в памяти.

Перенесено из AI_Host (bot/storage.py). Штатное хранилище aiogram держит всё в памяти:
перезапуск — и человек теряет, на каком он шаге. Redis ради одного пользователя
не нужен: один JSON-файл, атомарная запись, замок, битый файл не валит бота.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey

log = logging.getLogger("adreel.bot.storage")


class JsonFileStorage(BaseStorage):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._cache: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("ожидался объект")
            return data
        except (OSError, ValueError) as exc:
            log.warning("не смог прочитать %s (%s) — начинаю с чистого", self.path, exc)
            return {}

    def _flush(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self._cache, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            log.error("не смог сохранить состояние диалогов (%s)", exc)

    @staticmethod
    def _k(key: StorageKey) -> str:
        return f"{key.bot_id}:{key.chat_id}:{key.user_id}:{key.destiny}"

    async def set_state(self, key: StorageKey, state: str | State | None = None) -> None:
        async with self._lock:
            slot = self._cache.setdefault(self._k(key), {})
            slot["state"] = state.state if isinstance(state, State) else state
            self._flush()

    async def get_state(self, key: StorageKey) -> str | None:
        return self._cache.get(self._k(key), {}).get("state")

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        async with self._lock:
            self._cache.setdefault(self._k(key), {})["data"] = dict(data)
            self._flush()

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        return dict(self._cache.get(self._k(key), {}).get("data") or {})

    async def update_data(self, key: StorageKey, data: Mapping[str, Any]) -> dict[str, Any]:
        async with self._lock:
            slot = self._cache.setdefault(self._k(key), {})
            merged = {**(slot.get("data") or {}), **data}
            slot["data"] = merged
            self._flush()
            return dict(merged)

    async def close(self) -> None:
        async with self._lock:
            self._flush()

    def revive(self, busy_to_fallback: Mapping[str, str]) -> list[tuple[int, str]]:
        """Шаги «работаю» не переживают перезапуск: работы уже нет, а человек застрял бы
        навсегда. Возвращаем на шаг-замену. Результат — (чат, новый шаг): кого предупредить."""
        hurt: list[tuple[int, str]] = []
        for k, slot in self._cache.items():
            fallback = busy_to_fallback.get(slot.get("state") or "")
            if fallback is None:
                continue
            slot["state"] = fallback
            try:
                hurt.append((int(k.split(":")[1]), fallback))
            except (IndexError, ValueError):
                pass
        if hurt:
            self._flush()
        return hurt
