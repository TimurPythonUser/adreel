"""Реальный темп речи: сколько символов текста укладывается в секунду.

Перенесено из AI_Host (videonews/pace.py).

Зачем. Сценарист режет ролик на кадры ДО озвучки и должен знать, сколько текста
влезает в кадр. Число зависит от голоса, модели и ускорения, поэтому не задаётся
руками, а МЕРЯЕТСЯ: каждая удачная озвучка сообщает сюда длину в символах и секундах.
Сменили голос или темп — статистика обнуляется, чтобы старые числа не тянули назад.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .config import settings

log = logging.getLogger("adreel.pace")

#: стартовая оценка (симв/с), пока своих замеров нет. Украинский текст, из AI_Host.
DEFAULT_CHARS_PER_SEC = 14.5
#: меньше этого числа замеров статистике не доверяем
MIN_SAMPLES = 3
#: короткие куски меряют не темп, а паузы на входе и выходе
MIN_CHARS = 60


def _path() -> Path:
    return settings.workdir / "pace.json"


def _key() -> str:
    """Замеры действительны только для той же связки голос + модель + стабильность + темп."""
    return (f"{settings.elevenlabs_voice_id}@{settings.elevenlabs_model}"
            f"@s{settings.elevenlabs_stability:.2f}@{settings.tts_tempo:.3f}")


def _load() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and data.get("key") == _key() else {}


def observe(chars: int, seconds: float) -> None:
    """Запомнить факт: столько-то символов озвучено за столько-то секунд."""
    if chars < MIN_CHARS or seconds <= 0.1:
        return
    data = _load()
    data["key"] = _key()
    data["chars"] = data.get("chars", 0) + int(chars)
    data["seconds"] = round(data.get("seconds", 0.0) + float(seconds), 3)
    data["samples"] = data.get("samples", 0) + 1
    try:
        _path().parent.mkdir(parents=True, exist_ok=True)
        _path().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        log.warning("не смог сохранить замер темпа (%s)", exc)


def chars_per_sec() -> float:
    data = _load()
    if data.get("samples", 0) >= MIN_SAMPLES and data.get("seconds", 0) > 0:
        return float(data["chars"]) / float(data["seconds"])
    return DEFAULT_CHARS_PER_SEC * settings.tts_tempo


def describe() -> str:
    """Строка для промпта сценариста и логов."""
    data = _load()
    src = (f"замер по {data['samples']} озвучкам"
           if data.get("samples", 0) >= MIN_SAMPLES else "оценка")
    return f"{chars_per_sec():.1f} симв/с ({src})"
