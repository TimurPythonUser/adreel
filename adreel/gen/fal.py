"""fal.ai: очередь запросов с ожиданием и защитой от двойной оплаты.

Порядок, как в HeyGen-модуле AI_Host: запомнить request_id сразу после отправки →
опрашивать статус → забрать результат → скачать файл и записать стоимость → забыть.
Сбой на любом шаге до «забыть» НЕ выбрасывает генерацию: она уже оплачена, и повторная
попытка подберёт её по ключу входа. Поэтому забывает вызывающий (FalImageGen,
FalVideoGen) через done() — только когда файл уже у нас.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import fal_client

from .. import costs, net
from ..progress import say
from .base import ImageInput, Progress
from .models import ImageModel, VideoModel, pick
from .pending import PendingStore, fingerprint

log = logging.getLogger("adreel.gen.fal")


class GenerationFailed(RuntimeError):
    """Модель вернула ошибку — деньги за такой запрос обычно не списываются."""


class GenerationTimeout(TimeoutError):
    """Не дождались. Запрос запомнен: следующая попытка подберёт его бесплатно."""


class FalClient(Protocol):
    """То, что нам нужно от fal_client.AsyncClient (в тестах — подделка)."""

    async def submit(self, application: str, arguments: dict[str, Any]) -> Any: ...
    async def status(self, application: str, request_id: str) -> Any: ...
    async def result(self, application: str, request_id: str) -> Any: ...
    async def upload(self, data: bytes, content_type: str) -> str: ...


class FalGateway:
    def __init__(
        self,
        client: FalClient,
        store: PendingStore,
        *,
        poll_sec: float = 5.0,
        max_wait_sec: float = 20 * 60,
        max_status_errors: int = 10,
    ) -> None:
        self.client = client
        self.store = store
        self.poll_sec = poll_sec
        self.max_wait_sec = max_wait_sec
        self.max_status_errors = max_status_errors

    async def run(
        self,
        model_id: str,
        key: str,
        build_arguments: Callable[[], Awaitable[dict[str, Any]]],
        progress: Progress | None = None,
    ) -> Any:
        """Результат генерации (JSON модели).

        build_arguments вызывается ТОЛЬКО если придётся отправлять новый запрос:
        загрузка картинок на CDN fal тоже стоит времени, при подборе старого
        запроса она не нужна.
        """
        old = await self.store.get(key)
        if old and old.get("model") == model_id:
            reused = await self._try_reuse(model_id, key, old["request_id"], progress)
            if reused is not None:
                return reused

        arguments = await build_arguments()
        handle = await self.client.submit(model_id, arguments)
        request_id = handle.request_id
        # Запоминаем СРАЗУ: с этой секунды запрос может стоить денег.
        await self.store.remember(key, model_id, request_id)
        log.info("fal: %s -> %s", model_id, request_id)
        return await self._wait(model_id, key, request_id, progress)

    async def done(self, key: str) -> None:
        """Результат скачан и учтён — запись больше не нужна."""
        await self.store.forget(key)

    async def _try_reuse(
        self, model_id: str, key: str, request_id: str, progress: Progress | None
    ) -> Any | None:
        try:
            st = await self.client.status(model_id, request_id)
        except fal_client.FalClientError as exc:
            if getattr(exc, "status_code", None) in (404, 410):
                log.warning("старого запроса %s у fal больше нет", request_id)
                await self.store.forget(key)
                return None
            # 429 и 5xx временные: запрос, может, уже оплачен. Новый не шлём —
            # ждём старый, _wait сам переживёт серию ошибок статуса.
            log.warning("не смог проверить старый запрос %s (%s) — жду его",
                        request_id, str(exc)[:120])
            return await self._wait(model_id, key, request_id, progress)

        if isinstance(st, fal_client.Completed) and not st.error:
            await say(progress, "нашёл готовый результат прошлой попытки — забираю без оплаты")
            return await self.client.result(model_id, request_id)
        if isinstance(st, fal_client.Queued | fal_client.InProgress):
            await say(progress, "генерация с прошлой попытки ещё идёт — жду её, новую не запускаю")
            return await self._wait(model_id, key, request_id, progress)
        # ошибка или неизвестный статус — запись бесполезна
        await self.store.forget(key)
        return None

    async def _wait(
        self, model_id: str, key: str, request_id: str, progress: Progress | None
    ) -> Any:
        loop = asyncio.get_running_loop()
        started = loop.time()
        errors = 0
        while loop.time() - started < self.max_wait_sec:
            await asyncio.sleep(self.poll_sec)
            try:
                st = await self.client.status(model_id, request_id)
            except fal_client.FalClientError as exc:
                errors += 1
                log.warning("fal status %s: %s (%d подряд)", request_id, str(exc)[:120], errors)
                if errors >= self.max_status_errors:
                    raise GenerationTimeout(
                        f"fal не отвечает на статус {request_id}; запрос запомнен"
                    ) from exc
                continue
            errors = 0
            if isinstance(st, fal_client.Completed):
                if st.error:
                    await self.store.forget(key)
                    raise GenerationFailed(f"{model_id}: {st.error_type or ''} {st.error}".strip())
                return await self.client.result(model_id, request_id)
        raise GenerationTimeout(
            f"{model_id}: не дождался за {self.max_wait_sec / 60:.0f} мин. "
            "Запрос запомнен — повтор подберёт его без оплаты."
        )


class FalImageGen:
    def __init__(self, gateway: FalGateway, model: ImageModel,
                 text_model: ImageModel | None = None) -> None:
        self.gw = gateway
        self.model = model
        self.text_model = text_model

    async def generate(
        self,
        prompt: str,
        aspect_ratio: str | None = None,
        progress: Progress | None = None,
    ) -> bytes:
        model = self.text_model
        if model is None:
            raise GenerationFailed("нет модели картинок по тексту (image.text_default в "
                                   "config/video_models.yaml)")
        key = fingerprint(model.fal_id, prompt=prompt, aspect=aspect_ratio)

        async def build() -> dict[str, Any]:
            return model.arguments(image_urls=[], prompt=prompt, aspect_ratio=aspect_ratio)

        res = await self.gw.run(model.fal_id, key, build, progress)
        data = await net.download(pick(res, model.output), who="fal (картинка по тексту)")
        costs.record("fal", model.key, 1, "image", model.price_per_image)
        await self.gw.done(key)
        return data

    async def edit(
        self,
        images: list[ImageInput],
        prompt: str,
        aspect_ratio: str | None = None,
        progress: Progress | None = None,
    ) -> bytes:
        key = fingerprint(self.model.fal_id, prompt=prompt, aspect=aspect_ratio,
                          **{f"img{i}": im.data for i, im in enumerate(images)})

        async def build() -> dict[str, Any]:
            urls = [await self.gw.client.upload(im.data, im.mime) for im in images]
            return self.model.arguments(image_urls=urls, prompt=prompt, aspect_ratio=aspect_ratio)

        res = await self.gw.run(self.model.fal_id, key, build, progress)
        data = await net.download(pick(res, self.model.output), who="fal (картинка)")
        costs.record("fal", self.model.key, 1, "image", self.model.price_per_image)
        await self.gw.done(key)
        return data


class FalVideoGen:
    def __init__(self, gateway: FalGateway, model: VideoModel,
                 chain_model: VideoModel | None = None) -> None:
        self.gw = gateway
        self.model = model
        self.chain = chain_model

    @property
    def can_continue(self) -> bool:
        return self.chain is not None

    async def image_to_video(
        self,
        image: ImageInput,
        prompt: str,
        duration: int,
        end_image: ImageInput | None = None,
        seed: int | None = None,
        progress: Progress | None = None,
    ) -> bytes:
        key = fingerprint(self.model.fal_id, prompt=prompt, duration=duration, seed=seed,
                          image=image.data, end=end_image.data if end_image else None)

        async def build() -> dict[str, Any]:
            url = await self.gw.client.upload(image.data, image.mime)
            end_url = None
            if end_image is not None:
                end_url = await self.gw.client.upload(end_image.data, end_image.mime)
            return self.model.arguments(image_url=url, prompt=prompt, duration=duration,
                                        end_image_url=end_url, seed=seed)

        res = await self.gw.run(self.model.fal_id, key, build, progress)
        data = await net.download(pick(res, self.model.output), who="fal (видео)")
        costs.record("fal", self.model.key, duration, "video_sec", self.model.price(duration))
        await self.gw.done(key)
        return data

    async def continue_video(
        self,
        tail: bytes,
        images: list[ImageInput],
        prompt: str,
        duration: int,
        seed: int | None = None,
        progress: Progress | None = None,
    ) -> bytes:
        """Продолжение по референсам: хвост прошлого клипа + фото товара."""
        model = self.chain
        if model is None:
            raise GenerationFailed(f"{self.model.title}: нет модели продолжения по видео")
        key = fingerprint(model.fal_id, prompt=prompt, duration=duration, seed=seed, tail=tail,
                          **{f"ref{i}": im.data for i, im in enumerate(images)})

        async def build() -> dict[str, Any]:
            video_url = await self.gw.client.upload(tail, "video/mp4")
            image_urls = [await self.gw.client.upload(im.data, im.mime) for im in images]
            return model.reference_arguments(prompt=prompt, duration=duration,
                                             image_urls=image_urls, video_urls=[video_url],
                                             seed=seed)

        res = await self.gw.run(model.fal_id, key, build, progress)
        data = await net.download(pick(res, model.output), who="fal (продолжение)")
        costs.record("fal", model.key, duration, "video_sec",
                     model.price(duration, with_reference=True))
        await self.gw.done(key)
        return data
