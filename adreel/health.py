"""Проверка окружения при старте бота: чего не хватает в .env и конфигах.

Только локальные проверки без сети. Результат уходит в лог — владельцу в чат не шлём.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import load_yaml, settings
from .gen.models import image_model, video_model
from .render import ffmpeg


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


def preflight() -> list[Check]:
    s = settings
    checks = [
        Check("TELEGRAM_BOT_TOKEN", bool(s.telegram_bot_token)),
        Check("OWNER_TELEGRAM_ID", s.owner_telegram_id > 0, "бот пускает только владельца"),
        Check("ffmpeg/ffprobe", ffmpeg.available(), "нужны в PATH"),
        Check("TEXT_MODEL", bool(s.text_model)),
        Check("VISION_MODEL", bool(s.vision_model)),
        Check("FAL_KEY", bool(s.fal_key)),
        Check("ElevenLabs", bool(s.elevenlabs_api_key and s.elevenlabs_voice_id
                                 and s.elevenlabs_model),
              "ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID, ELEVENLABS_MODEL"),
    ]
    prices = load_yaml("pricing.yaml").get("llm", {})
    # креативщик — самый частый LLM-вызов; без цены он молча пишется в учёт с $0
    for m in {s.text_model, s.vision_model, s.creative_model} - {""}:
        checks.append(Check(f"цена {m}", m in prices, "добавь в config/pricing.yaml"))
        if not m.lower().startswith("deepseek"):
            checks.append(Check("ANTHROPIC_API_KEY", bool(s.anthropic_api_key)))
        else:
            checks.append(Check("DEEPSEEK_API_KEY", bool(s.deepseek_api_key)))
    try:
        v, i = video_model(), image_model()
        checks.append(Check("модели fal", True, f"видео: {v.title}, картинки: {i.title}"))
    except (KeyError, ValueError) as exc:
        checks.append(Check("модели fal", False, str(exc)))
    return checks
