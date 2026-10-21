"""Настройки: ключи и имена моделей — из .env, цены и каталоги моделей — из config/*.yaml.

В коде нет ни ключей, ни цен, ни названий моделей: сменить модель или тариф можно
правкой конфига, без правки кода.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---------- Telegram ----------
    telegram_bot_token: str = ""
    # Бот личный: пускаем ровно одного человека. 0 = никого (защита от случайного запуска).
    owner_telegram_id: int = 0
    # Локальный Bot API (docker-compose): снимает лимит 20 МБ. Пусто = облачный api.telegram.org.
    telegram_api_base: str = ""

    # ---------- LLM ----------
    anthropic_api_key: str = ""
    deepseek_api_key: str = ""
    # Имена моделей только из .env. Всё, что начинается с "deepseek", идёт в DeepSeek.
    text_model: str = ""
    vision_model: str = ""
    # Креативщик, сценарист и копирайтер. Пусто = TEXT_MODEL. Творческая часть решает
    # качество ролика — сюда имеет смысл ставить модель сильнее текстовой.
    creative_model: str = ""

    # ---------- Озвучка ----------
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    elevenlabs_model: str = ""
    elevenlabs_stability: float = 1.0
    elevenlabs_similarity: float = 0.75
    # Ускорение готовой озвучки через ffmpeg atempo. 1.0 = без ускорения.
    tts_tempo: float = 1.0
    # Расшифровка голосовых (ElevenLabs Scribe)
    stt_model: str = "scribe_v2"

    # ---------- Генерация картинок и видео ----------
    fal_key: str = ""
    # Поиск в интернете для креативщика (tavily.com). Пусто = работаем без поиска.
    tavily_api_key: str = ""
    # Ключи из config/video_models.yaml; пусто = default из файла.
    video_model: str = ""
    image_model: str = ""

    # ---------- Пути и режимы ----------
    workdir: Path = ROOT / "workdir"
    config_dir: Path = ROOT / "config"
    prompts_dir: Path = ROOT / "prompts"
    fonts_dir: Path = ROOT / "assets" / "fonts"
    # Пусто = SQLite в workdir/adreel.db. Переезд на Postgres — сменить URL.
    database_url: str = ""
    run_live_tests: bool = False

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite+aiosqlite:///{(self.workdir / 'adreel.db').as_posix()}"

    @property
    def media_dir(self) -> Path:
        return self.workdir / "media"

    @field_validator("owner_telegram_id", mode="before")
    @classmethod
    def _empty_id_is_zero(cls, v: object) -> object:
        """OWNER_TELEGRAM_ID= (пусто, как в .env.example) — «ещё не задан», а не ошибка старта:
        без запущенного бота id не узнать (/id)."""
        return 0 if isinstance(v, str) and not v.strip() else v


settings = Settings()


@lru_cache(maxsize=16)
def load_yaml(name: str) -> dict[str, Any]:
    """YAML из config/. Кешируется: конфиги читаются один раз за запуск."""
    path = settings.config_dir / name
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: ожидался словарь на верхнем уровне")
    return data
