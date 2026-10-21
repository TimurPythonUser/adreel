"""Товар: материалы, разбор, анкета, правки. Бот — тонкая оболочка над этим сервисом."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .. import costs
from ..db.models import CostEventRow, Material, Product
from ..ingest import pages, video
from ..ingest.media import MediaStore, NotAnImage, inspect_image
from ..ingest.stt import STTProvider
from ..llm.base import LLMProvider
from ..progress import Progress, say
from . import extract
from .questions import Question, apply_answer, pending_questions
from .schema import ImageNote, ProductCard, Suggestions

log = logging.getLogger("adreel.product.service")

#: картинки со страницы мельче этого — иконки и превьюшки, товар на них не разглядеть
MIN_PAGE_IMAGE_SIDE = 300
#: фото мельче этого принимаем, но предупреждаем: в кадр 1080x1920 такое не годится
LOW_RES_SIDE = 800


#: универсальный запасной путь для любой ссылки: зрение читает текст со скриншотов
SCREENSHOT_HINT = ("📸 Сделай 2–3 скриншота страницы товара (название и цена, описание, "
                   "характеристики) и пришли сюда — прочитаю с картинок. "
                   "Или скопируй описание текстом.")


class ProductError(RuntimeError):
    """Ошибка, которую можно показать владельцу как есть."""


@dataclass
class Added:
    kind: str
    note: str = ""          # что сказать владельцу («фото маленькое» и т.п.)
    duplicate: bool = False
    extra: int = 0          # сколько картинок подтянули со страницы


@dataclass
class Analysis:
    card: ProductCard
    suggestions: Suggestions
    questions: list[Question]
    doubts: list[str] = field(default_factory=list)


@dataclass
class Deps:
    sessions: async_sessionmaker[AsyncSession]
    media: MediaStore
    text_llm: Callable[[], LLMProvider]
    vision_llm: Callable[[], LLMProvider]
    stt: Callable[[], STTProvider]
    fetch_page: Callable[[str], Awaitable[pages.PageData]] = pages.fetch_page
    fetch_image: Callable[[str], Awaitable[bytes]] | None = None
    download_video: Callable[[str], Awaitable[bytes]] = video.download_url
    video_frames: Callable[[bytes], Awaitable[list[bytes]]] = video.frames
    today: Callable[[], date] = field(default=lambda: datetime.now(UTC).date())


async def _fetch_image(url: str) -> bytes:
    r = await pages.fetch(url, max_bytes=15 * 1024 * 1024, who="картинка", accept="image/*")
    return r.content


class ProductService:
    def __init__(self, deps: Deps) -> None:
        self.d = deps
        self._fetch_image = deps.fetch_image or _fetch_image

    # ---------- товары ----------

    async def recent(self, owner_id: int, limit: int = 6) -> list[Product]:
        async with self.d.sessions() as s:
            rows = await s.scalars(
                select(Product).where(Product.owner_id == owner_id, Product.status == "ready")
                .order_by(Product.last_used_at.desc()).limit(limit))
            return list(rows)

    async def new_draft(self, owner_id: int, language: str) -> int:
        card = ProductCard.model_validate(
            {"language": language if language in ("uk", "ru", "en") else "uk"})
        async with self.d.sessions() as s:
            p = Product(owner_id=owner_id, card=card.model_dump())
            s.add(p)
            await s.commit()
            return p.id

    async def card(self, pid: int) -> ProductCard:
        async with self.d.sessions() as s:
            p = await self._product(s, pid)
            return ProductCard.model_validate(p.card)

    async def save_card(self, pid: int, card: ProductCard) -> None:
        async with self.d.sessions() as s:
            p = await self._product(s, pid)
            p.card = card.model_dump()
            p.name = card.name[:255]
            await s.commit()

    async def confirm(self, pid: int) -> ProductCard:
        async with self.d.sessions() as s:
            p = await self._product(s, pid)
            card = ProductCard.model_validate(p.card)
            if not card.name:
                raise ProductError("у товара нет названия — ответь на вопрос или поправь карточку")
            p.status = "ready"
            p.last_used_at = datetime.now(UTC)
            await s.commit()
            return card

    async def counts(self, pid: int) -> dict[str, int]:
        async with self.d.sessions() as s:
            rows = await s.execute(
                select(Material.kind, func.count()).where(Material.product_id == pid)
                .group_by(Material.kind))
            return {k: int(n) for k, n in rows.all()}

    @staticmethod
    async def _product(s: AsyncSession, pid: int) -> Product:
        p = await s.get(Product, pid)
        if p is None:
            raise ProductError("товар не найден — начни заново: /new")
        return p

    # ---------- материалы ----------

    async def add_text(self, pid: int, text: str, kind: str = "text") -> Added:
        text = text.strip()
        if not text:
            raise ProductError("пустой текст")
        async with self.d.sessions() as s:
            s.add(Material(product_id=pid, kind=kind, source="telegram", text=text))
            await s.commit()
        return Added(kind)

    async def add_voice(self, pid: int, audio: bytes, filename: str, seconds: float,
                        language: str | None = None) -> str:
        async with self.spend(pid, "materials"):
            text = await self.d.stt().transcribe(audio, filename, seconds, language)
        if not text:
            raise ProductError("не разобрал голосовое — попробуй ещё раз или напиши текстом")
        await self.add_text(pid, text, kind="voice")
        return text

    async def transcribe(self, pid: int, audio: bytes, filename: str, seconds: float,
                         language: str | None = None) -> str:
        """Голосовой ответ на вопрос или правка: расшифровать, но в материалы не класть."""
        async with self.spend(pid, "answers"):
            text = await self.d.stt().transcribe(audio, filename, seconds, language)
        if not text:
            raise ProductError("не разобрал голосовое — попробуй ещё раз или напиши текстом")
        return text

    async def add_photo(self, pid: int, data: bytes, source: str = "telegram",
                        meta: dict | None = None, kind: str = "photo") -> Added:
        try:
            img = await asyncio.to_thread(inspect_image, data)
        except NotAnImage as exc:
            raise ProductError(str(exc)) from exc
        stored = await self.d.media.save_async(img.data, img.mime)
        async with self.d.sessions() as s:
            dup = await s.scalar(select(Material.id).where(
                Material.product_id == pid, Material.sha256 == stored.sha256))
            if dup:
                return Added(kind, "такое фото уже есть", duplicate=True)
            s.add(Material(product_id=pid, kind=kind, source=source, file_path=stored.rel_path,
                           sha256=stored.sha256, mime=img.mime, width=img.width,
                           height=img.height, meta=meta or {}))
            await s.commit()
        note = ""
        if kind == "photo" and min(img.width, img.height) < LOW_RES_SIDE:
            note = (f"фото маленькое ({img.width}×{img.height}) — для ролика лучше "
                    f"от {LOW_RES_SIDE}px по короткой стороне")
        return Added(kind, note)

    async def add_video(self, pid: int, data: bytes, source: str = "telegram",
                        mime: str = "video/mp4") -> Added:
        stored = await self.d.media.save_async(data, mime)
        async with self.d.sessions() as s:
            if await s.scalar(select(Material.id).where(
                    Material.product_id == pid, Material.sha256 == stored.sha256)):
                return Added("video", "это видео уже есть", duplicate=True)
            s.add(Material(product_id=pid, kind="video", source=source,
                           file_path=stored.rel_path, sha256=stored.sha256, mime=mime))
            await s.commit()
        n = 0
        try:
            for frame in await self.d.video_frames(data):
                res = await self.add_photo(pid, frame, source=source, kind="frame",
                                           meta={"from_video": stored.sha256})
                n += 0 if res.duplicate else 1
        except Exception as exc:
            log.warning("не смог вытащить кадры: %s", exc)
        return Added("video", extra=n)

    async def add_link(self, pid: int, url: str) -> Added:
        host = pages.host_of(url)
        if pages.is_social_url(url):
            raise ProductError(f"видео из {host} по ссылке не скачать — соцсеть пускает только "
                               "своих. Сохрани видео на телефон и пришли сюда файлом.")
        if pages.is_video_url(url):
            try:
                data = await self.d.download_video(url)
            except video.VideoError as exc:
                raise ProductError(f"{exc}. Если видео у тебя есть — пришли файлом.") from exc
            return await self.add_video(pid, data, source=url)
        try:
            page = await self.d.fetch_page(url)
        except pages.PageBlocked as exc:
            raise ProductError(f"сайт {host} не пускает ботов.\n{SCREENSHOT_HINT}") from exc
        except pages.PageEmpty as exc:
            raise ProductError(f"страница {host} собирается скриптами — текста для меня "
                               f"там нет.\n{SCREENSHOT_HINT}") from exc
        except Exception as exc:
            raise ProductError(f"не открыл ссылку ({str(exc)[:120]}).\n{SCREENSHOT_HINT}") from exc
        text = pages.page_as_text(page)
        async with self.d.sessions() as s:
            s.add(Material(product_id=pid, kind="link", source=url, text=text,
                           meta={"title": page.title, "price": page.price,
                                 "brand": page.brand, "images": page.images,
                                 "jsonld": bool(page.jsonld)}))
            await s.commit()
        added = 0
        for img_url in page.images:
            try:
                data = await self._fetch_image(img_url)
                info = await asyncio.to_thread(inspect_image, data)
            except Exception as exc:
                log.info("картинка %s пропущена: %s", img_url, str(exc)[:100])
                continue
            if min(info.width, info.height) < MIN_PAGE_IMAGE_SIDE:
                continue
            res = await self.add_photo(pid, info.data, source=img_url, meta={"from_page": url})
            added += 0 if res.duplicate else 1
        note = "" if text.strip() else "текста на странице почти нет"
        return Added("link", note, extra=added)

    # ---------- разбор ----------

    async def analyze(self, pid: int, progress: Progress | None = None) -> Analysis:
        async with self.d.sessions() as s:
            product = await self._product(s, pid)
            materials = list(await s.scalars(
                select(Material).where(Material.product_id == pid).order_by(Material.id)))
            existing = ProductCard.model_validate(product.card)
        if not materials:
            raise ProductError("материалов нет — пришли ссылку, фото или текст")

        has_content = bool(existing.name or existing.benefits or existing.what_it_is)
        async with self.spend(pid, "analyze"):
            notes = await self._vision_notes(pid, materials, existing.language, progress)
            texts = [extract.TextSource(tag=f"{_tag(m.kind)}:{m.id}", text=m.text,
                                        title=str(m.meta.get("title", "")))
                     for m in materials if m.text]
            await say(progress, "✍️ Собираю карточку товара…")
            res = await extract.build_card(self.d.text_llm(), texts, notes,
                                           language=existing.language,
                                           existing=existing if has_content else None)
        # answered — это история ответов владельца, её не отдаём на откуп модели
        res.card.answered = list(existing.answered)
        await self.save_card(pid, res.card)
        return Analysis(res.card, res.suggestions,
                        pending_questions(res.card, res.suggestions), res.doubts)

    async def _vision_notes(self, pid: int, materials: list[Material], language: str,
                            progress: Progress | None) -> dict[int, ImageNote]:
        """Разбор фото кешируется в meta материала: повторный анализ не платит за старые фото."""
        notes: dict[int, ImageNote] = {}
        todo: list[extract.ImageSource] = []
        for m in materials:
            if m.kind not in ("photo", "frame"):
                continue
            if "vision" in m.meta:
                notes[m.id] = ImageNote.model_validate(m.meta["vision"])
            else:
                todo.append(extract.ImageSource(m.id, await self.d.media.read_async(m.file_path)))
        if todo:
            await say(progress, f"👀 Смотрю фото ({len(todo)})…")
            fresh = await extract.describe_images(self.d.vision_llm(), todo, language)
            notes.update(fresh)
            async with self.d.sessions() as s:
                for mid, note in fresh.items():
                    row = await s.get(Material, mid)
                    if row is not None:
                        row.meta = {**row.meta, "vision": note.model_dump()}
                await s.commit()
        return notes

    async def product_photos(self, pid: int) -> list[Material]:
        """Фото, на которых сам товар, — сырьё для стартовых кадров (этап 4)."""
        async with self.d.sessions() as s:
            rows = await s.scalars(select(Material).where(
                Material.product_id == pid, Material.kind == "photo").order_by(Material.id))
            return [m for m in rows if m.meta.get("vision", {}).get("is_product", True)]

    async def owner_notes(self, pid: int, limit_chars: int = 3000) -> list[str]:
        """Тексты и голосовые владельца дословно.

        В карточку попадают факты о товаре, а пожелания к ролику («хочу динамично,
        девушка в машине») там теряются. Креативщику и режиссёру они нужны как есть.
        """
        async with self.d.sessions() as s:
            rows = await s.scalars(select(Material).where(
                Material.product_id == pid, Material.kind.in_(("text", "voice")))
                .order_by(Material.id))
            out, budget = [], limit_chars
            for m in rows:
                chunk = m.text.strip()[:budget]
                if chunk:
                    out.append(chunk)
                    budget -= len(chunk)
                if budget <= 0:
                    break
            return out

    # ---------- анкета и правки ----------

    async def answer(self, pid: int, field_name: str, value: str) -> ProductCard:
        card = apply_answer(await self.card(pid), field_name, value, year=self.d.today().year)
        await self.save_card(pid, card)
        return card

    async def edit(self, pid: int, instruction: str) -> ProductCard:
        card = await self.card(pid)
        async with self.spend(pid, "edit"):
            new = await extract.edit_card(self.d.text_llm(), card, instruction)
        await self.save_card(pid, new)
        return new

    # ---------- затраты ----------

    @contextlib.asynccontextmanager
    async def spend(self, pid: int, stage: str) -> AsyncIterator[costs.Ledger]:
        """Всё, что потратят вызовы внутри, пишется в cost_events — даже если шаг упал."""
        ledger = costs.Ledger(f"product:{pid}")
        try:
            with costs.job_scope(ledger):
                yield ledger
        finally:
            if ledger.events:
                async with self.d.sessions() as s:
                    s.add_all(CostEventRow(product_id=pid, stage=stage, provider=e.provider,
                                           operation=e.operation, units=e.units,
                                           unit_type=e.unit_type, usd=e.usd)
                              for e in ledger.events)
                    await s.commit()

    async def spent_usd(self, pid: int) -> float:
        async with self.d.sessions() as s:
            total = await s.scalar(select(func.sum(CostEventRow.usd))
                                   .where(CostEventRow.product_id == pid))
            return float(total or 0.0)


def _tag(kind: str) -> str:
    return {"link": "page"}.get(kind, kind)
