"""Сборка карточки товара из материалов: зрение разбирает фото, текстовая модель — всё вместе."""

from __future__ import annotations

import io
import json
import logging
from dataclasses import dataclass

from PIL import Image

from .. import prompts
from ..config import load_yaml
from ..llm.base import LLMProvider, image_block, text_block
from .schema import CardExtraction, ImageNote, ImageNotes, ProductCard

log = logging.getLogger("adreel.product.extract")

#: столько фото отдаём зрению за один вызов (дальше токены растут, толку мало)
MAX_IMAGES = 8
#: длинная сторона картинки для зрения: деталей хватает, токенов в разы меньше
VISION_MAX_SIDE = 1024
#: сколько текста одной страницы и всего вместе отдаём модели
PAGE_CHARS = 6000
TOTAL_CHARS = 24000


@dataclass(frozen=True)
class TextSource:
    """Текстовый материал: страница, сообщение, расшифровка голосового."""

    tag: str        # "page:3", "text:4", "voice:5"
    text: str
    title: str = ""


@dataclass(frozen=True)
class ImageSource:
    material_id: int
    data: bytes


def categories() -> dict[str, dict[str, str]]:
    return load_yaml("categories.yaml")["categories"]


def shrink_for_vision(data: bytes) -> tuple[bytes, str]:
    """JPEG не больше VISION_MAX_SIDE. Заодно приводит WEBP/AVIF/PNG с прозрачностью к JPEG."""
    with Image.open(io.BytesIO(data)) as src:
        rgb = src.convert("RGB")
        rgb.thumbnail((VISION_MAX_SIDE, VISION_MAX_SIDE))
        buf = io.BytesIO()
        rgb.save(buf, "JPEG", quality=85)
    return buf.getvalue(), "image/jpeg"


async def describe_images(vision: LLMProvider, images: list[ImageSource],
                          language: str) -> dict[int, ImageNote]:
    """{material_id: разбор}. Номера в промпте — id материалов, чтобы источник был однозначным."""
    notes: dict[int, ImageNote] = {}
    for start in range(0, len(images), MAX_IMAGES):
        batch = images[start:start + MAX_IMAGES]
        content: list[dict] = []
        for img in batch:
            data, mime = shrink_for_vision(img.data)
            content.append(text_block(f"Фото {img.material_id}:"))
            content.append(image_block(data, mime))
        system = prompts.render("product_images", first_index=batch[0].material_id,
                                language=language)
        system += "\nНомера фото: " + ", ".join(str(i.material_id) for i in batch) + "."
        # Скриншоты страниц дают до 1500 символов текста на картинку — лимит с запасом.
        res = await vision.parse(system, content, ImageNotes, max_tokens=12000)
        ids = {i.material_id for i in batch}
        for n in res.notes:
            if n.index in ids:
                notes[n.index] = n
    return notes


def _materials_block(texts: list[TextSource], notes: dict[int, ImageNote]) -> str:
    parts: list[str] = []
    budget = TOTAL_CHARS
    for t in texts:
        chunk = t.text.strip()[:min(PAGE_CHARS, max(budget, 0))]
        if not chunk:
            continue
        budget -= len(chunk)
        head = f"[{t.tag}]" + (f" {t.title}" if t.title else "")
        parts.append(f"{head}\n{chunk}")
    for mid, n in sorted(notes.items()):
        text = f" Надписи: {n.visible_text}" if n.visible_text else ""
        kind = "товар" if n.is_product else "не товар"
        parts.append(f"[photo:{mid}] ({kind}, качество {n.quality}) {n.description}{text}")
    return "\n\n".join(parts) or "(материалов нет)"


async def build_card(llm: LLMProvider, texts: list[TextSource], notes: dict[int, ImageNote],
                     *, language: str, existing: ProductCard | None = None) -> CardExtraction:
    cats = categories()
    cat_lines = "\n".join(f"- {k}: {v['title']}. {v.get('hints', '')}".rstrip()
                          for k, v in cats.items())
    existing_block = ""
    if existing is not None:
        existing_block = (
            "Текущая карточка (обнови её новыми материалами; поля из answered заполнил "
            "владелец — НЕ меняй их):\n" + existing.model_dump_json(indent=1)
        )
    system = prompts.render("product_card", language=language, categories=cat_lines,
                            existing=existing_block)
    user = "МАТЕРИАЛЫ О ТОВАРЕ:\n\n" + _materials_block(texts, notes)
    res = await llm.parse(system, user, CardExtraction, max_tokens=8000)
    return _guard(res, existing, cats)


def _guard(res: CardExtraction, existing: ProductCard | None,
           cats: dict[str, dict[str, str]]) -> CardExtraction:
    """Страховка поверх промпта: ответы владельца важнее того, что решила модель."""
    card = res.card
    if card.category not in cats:
        log.info("неизвестная категория %r -> other", card.category)
        card.category = "other"
    if existing is not None:
        for name in existing.answered:
            setattr(card, name, getattr(existing, name))
        card.answered = list(existing.answered)
        # факты от владельца модель могла потерять при пересборке — возвращаем
        for listname in ("benefits", "specs"):
            have = {f.text for f in getattr(card, listname)}
            for f in getattr(existing, listname):
                if f.source == "user" and f.text not in have:
                    getattr(card, listname).append(f)
    return res


async def edit_card(llm: LLMProvider, card: ProductCard, instruction: str) -> ProductCard:
    """Правка карточки словами владельца («цена 799», «аудитория — мамы»)."""
    # Карточка — в user-сообщение в рамке: её собирала LLM из чужих страниц (см. brief_message)
    system = prompts.render("product_card_edit", category_keys=", ".join(categories()))
    user = (f"<карточка>\n{card.model_dump_json(indent=1)}\n</карточка>\n\n"
            f"Просьба владельца: {instruction}")
    new = await llm.parse(system, user, ProductCard, max_tokens=6000)
    if new.category not in categories():
        new.category = card.category
    new.answered = sorted(set(card.answered) | set(new.answered))
    return new


def card_json(card: ProductCard) -> str:
    return json.dumps(card.model_dump(), ensure_ascii=False)
