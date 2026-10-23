"""Генерация картинок и видео (fal.ai)."""

from __future__ import annotations

from functools import lru_cache

import fal_client

from ..config import settings
from .fal import FalGateway, FalImageGen, FalVideoGen
from .models import image_model, text_image_model, video_model
from .pending import PendingStore


@lru_cache(maxsize=1)
def _pending() -> PendingStore:
    """Один на процесс: у каждого стора свой Lock, и два стора на один файл
    затирали бы записи друг друга при параллельной записи."""
    return PendingStore(settings.workdir / "fal_pending.json")


def make_gateway() -> FalGateway:
    if not settings.fal_key:
        raise RuntimeError("не задан FAL_KEY (.env)")
    client = fal_client.AsyncClient(key=settings.fal_key)
    return FalGateway(client, _pending())


def make_video_gen(model_key: str | None = None) -> FalVideoGen:
    """Основная модель плюс модель бесшовного продолжения, если она задана в каталоге."""
    model = video_model(model_key)
    chain = video_model(model.chain_model) if model.chain_model else None
    return FalVideoGen(make_gateway(), model, chain)


def make_image_gen(model_key: str | None = None) -> FalImageGen:
    return FalImageGen(make_gateway(), image_model(model_key), text_image_model())
