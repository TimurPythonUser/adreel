"""Стартовые кадры клипов: реальное фото товара или сцена, сгенерированная по нему.

Видеомодель берёт размер холста от стартовой картинки, поэтому все кадры приводим
к вертикали 9:16 заранее — иначе ролик соберётся из разных форматов.

Непрерывность. Продолжающая сцена стартует с последнего кадра предыдущего клипа, и
на длинной цепочке копится дрейф цвета: каждый клип чуть «подкручивает» картинку, и
к четвёртой сцене это уже видно. Поэтому кадр перед отправкой подгоняем по палитре к
якорю — первому кадру ролика (приём из практики: color-match хвоста перед передачей).
"""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass

from PIL import Image, ImageStat

from ..render import ffmpeg
from .base import ImageGenProvider, ImageInput, Progress

#: вертикаль под TikTok/Reels/Shorts
ASPECT = "9:16"
FRAME_W, FRAME_H = 1080, 1920
#: насколько сильно разрешаем тянуть канал при подгонке палитры
MAX_GAIN = 1.25


def to_vertical(data: bytes, width: int = FRAME_W, height: int = FRAME_H) -> bytes:
    """Кадрирование под 9:16 «по заполнению»: увеличиваем и срезаем лишнее по центру.

    Товар почти всегда в середине кадра, поэтому центральный срез безопаснее полей:
    поля модель дорисует как часть сцены, и в ролике появятся рамки.
    """
    with Image.open(io.BytesIO(data)) as src:
        im = src.convert("RGB")
        scale = max(width / im.width, height / im.height)
        im = im.resize((max(width, round(im.width * scale)), max(height, round(im.height * scale))))
        left, top = (im.width - width) // 2, (im.height - height) // 2
        im = im.crop((left, top, left + width, top + height))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=92)
    return buf.getvalue()


async def vertical(data: bytes) -> bytes:
    """to_vertical в потоке: PIL на кадре 1080x1920 — десятки мс, event loop не держим."""
    return await asyncio.to_thread(to_vertical, data)


async def last_frame(clip: bytes, anchor: bytes | None = None) -> bytes:
    """Последний кадр клипа — стартовый для следующего, если сцены идут без склейки."""
    frames = await ffmpeg.tail_frames(clip, count=1)
    if not frames:
        raise ValueError("не смог достать последний кадр клипа")
    frame = await vertical(frames[0])
    return await asyncio.to_thread(match_palette, frame, anchor) if anchor else frame


def match_palette(frame: bytes, anchor: bytes) -> bytes:
    """Подтянуть средние по каналам к якорному кадру — против дрейфа цвета в цепочке.

    Линейное усиление каждого канала, ограниченное MAX_GAIN: задача — снять уползание
    в тепло или в синеву, а не перекрасить сцену (в ней законно может смениться свет).
    """
    with Image.open(io.BytesIO(frame)) as f, Image.open(io.BytesIO(anchor)) as a:
        cur, ref = f.convert("RGB"), a.convert("RGB")
        means = ImageStat.Stat(cur).mean
        goal = ImageStat.Stat(ref).mean
        gains = [min(MAX_GAIN, max(1 / MAX_GAIN, g / m)) if m > 1 else 1.0
                 for m, g in zip(means, goal, strict=True)]
        if all(abs(g - 1.0) < 0.02 for g in gains):
            return frame
        fixed = Image.merge("RGB", [ch.point(lambda v, g=g: min(255, round(v * g)))
                                    for ch, g in zip(cur.split(), gains, strict=True)])
        buf = io.BytesIO()
        fixed.save(buf, "JPEG", quality=92)
    return buf.getvalue()


async def tail_clip(clip: bytes, seconds: float) -> bytes:
    """Хвост клипа как отдельный MP4 — референс движения для бесшовного продолжения."""
    return await ffmpeg.tail_video(clip, seconds)


#: что не менять в товаре. Урок из ТЗ: генеративные модели любят «улучшить» упаковку
#: и надписи — тогда в рекламе окажется не тот товар, который продаём
PRODUCT_CONTRACT = ("Keep it exactly as it is: same shape, proportions, colors, materials, "
                    "label and packaging, same number of parts. Do not redesign it, do not add "
                    "or change any text or logo.")


@dataclass(frozen=True)
class Person:
    """Герой в кадре. frame — его первая картинка: уходит референсом, чтобы во всех
    сценах был один человек. None — герой появляется впервые, рисуем по описанию."""

    name: str
    look: str
    frame: bytes | None = None


async def make_scene(imagegen: ImageGenProvider, prompt: str, *, product: bytes | None = None,
                     people: list[Person] | None = None, style: str = "",
                     progress: Progress | None = None) -> bytes:
    """Стартовый кадр сцены.

    product — фото товара, если товар в кадре: форма товара держится по референсу.
    Сцена без товара рисуется БЕЗ фото товара — раньше оно уходило в каждую картинку,
    и товар оказывался в каждом кадре: ролик выходил одинаковым (урок 22.10.2026).
    Референсов нет совсем — картинка по тексту.
    """
    images: list[ImageInput] = []
    refs: list[str] = []
    if product is not None:
        images.append(ImageInput(await vertical(product), "image/jpeg"))
        refs.append(f"Image {len(images)} is the product. {PRODUCT_CONTRACT}")
    for p in people or []:
        if p.frame is not None:
            images.append(ImageInput(p.frame, "image/jpeg"))
            refs.append(f"Image {len(images)} shows {p.name}: the same person — keep the face, "
                        "age, hairstyle, body and outfit identical.")
    text = _scene_prompt(prompt, refs, people or [], style, with_product=product is not None)
    if images:
        raw = await imagegen.edit(images, text, aspect_ratio=ASPECT, progress=progress)
    else:
        raw = await imagegen.generate(text, aspect_ratio=ASPECT, progress=progress)
    return await vertical(raw)


def _scene_prompt(scene: str, refs: list[str], people: list[Person], style: str, *,
                  with_product: bool) -> str:
    """Роли референсов идут ПЕРВЫМИ, дальше — что нового в кадре.

    Так просит официальный гайд H3 и так же ведут себя модели картинок: первое
    требование весит больше хвоста. Внешность героя — той же фразой, что в сценарии,
    в каждой его сцене: одинаковое описание держит одного человека.
    """
    parts = ["Vertical 9:16 photorealistic film still from a commercial.", *refs]
    parts += [f"{p.name}: {p.look.rstrip('. ')}." for p in people]
    parts.append(f"Scene: {scene.rstrip('. ')}.")
    if not with_product:
        parts.append("The advertised product does not appear in this frame.")
    if style:
        parts.append(f"Look: {style.rstrip('. ')}.")
    parts.append("Do not add captions, watermarks or any text beyond what is printed on "
                 "objects.")
    return " ".join(parts)
