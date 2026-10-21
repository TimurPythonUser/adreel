"""Тексты и кнопки бота."""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..db.models import Product, Track
from ..product.extract import categories
from ..product.questions import Question
from ..product.schema import ProductCard

if TYPE_CHECKING:
    from ..music.service import Choice
    from ..script.schema import Direction, VideoScript, Voiceover

LANG_TITLES = {"uk": "украинский", "ru": "русский", "en": "английский"}
KIND_TITLES = {"photo": "фото", "link": "ссылки", "text": "тексты", "voice": "голосовые",
               "video": "видео", "frame": "кадры из видео"}


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


def choose_product(products: list[Product]) -> tuple[str, InlineKeyboardMarkup]:
    rows = []
    for p in products:
        when = _ago(p.last_used_at)
        rows.append([(f"📦 {p.name or 'без названия'} · {when}"[:60], f"p:{p.id}")])
    rows.append([("➕ Новый товар", "p:new")])
    return "С каким товаром работаем?", kb(rows)


def _ago(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone().strftime("%d.%m")


HELP = (
    "🎬 <b>AdReel — рекламные AI-ролики для товаров</b>\n\n"
    "Я делаю короткие ролики (до 30 секунд) для TikTok, Reels и Shorts. "
    "Ты даёшь материалы о товаре — я разбираю товар, пишу сценарий, генерирую видео "
    "нейросетью, озвучиваю, добавляю субтитры и музыку.\n\n"
    "<b>Как пользоваться</b>\n"
    "1️⃣ /new — выбери сохранённый товар или заведи новый.\n"
    "2️⃣ Закинь материалы, что есть:\n"
    "   🔗 ссылку на товар в магазине — вытащу название, цену, описание и фото. "
    "Если сайт не пустит, скажу — тогда пришли скриншоты страницы;\n"
    "   📸 фото товара или скриншоты страницы (лучше файлом — без сжатия);\n"
    "   ✍️ текст или 🎙 голосовое: что за товар, чем хорош, что хочешь видеть в ролике;\n"
    "   🎬 видео файлом или ссылкой на YouTube. Из Instagram и TikTok — только файлом.\n"
    "3️⃣ Жми «Готово» — я разберу всё и покажу карточку товара.\n"
    "4️⃣ Спрошу то, чего не хватает. Можно жать кнопку-вариант, "
    "«✏️ Свой вариант» или просто написать / надиктовать ответ.\n"
    "5️⃣ Проверь карточку: «Всё верно», «Поправить» (словами, например «цена 799») "
    "или «Добавить материалы».\n"
    "6️⃣ Креативщик предложит несколько направлений ролика — выбери или дай свой вариант. "
    "Потом длина: 15, 20 или 30 секунд.\n"
    "7️⃣ Режиссёр пишет сценарий по сценам — каждая сцена станет отдельным видеоклипом "
    "на 4–10 секунд. Можно поправить словами, переделать сцену или взять другой вариант.\n"
    "8️⃣ Копирайтер пишет озвучку под готовые сцены — чтобы текст влез по времени.\n"
    "9️⃣ Музыка: пришли свой трек файлом или ссылкой на YouTube. Подберу кусок под "
    "сценарий (дроп — на главный кадр) и пришлю послушать. Кусок можно выбрать самому: "
    "«0:42–1:02», «припев», «вступление».\n\n"
    "🔟 Итог: сценарий, озвучка и трек одним сообщением. Тут последняя возможность "
    "поменять сценарий, озвучку или трек. Жмёшь «Утвердить» — дальше начинается "
    "платная генерация.\n\n"
    "1️⃣1️⃣ Генерация и монтаж идут сами: каждая сцена превращается в видеоклип, потом "
    "монтаж склеивает всё в один ролик — голос, субтитры, музыка тише голоса. "
    "Куски я не присылаю: в чат приходит готовое видео. Не понравилось — "
    "«Сгенерировать заново» или «Поменять сценарий».\n\n"
    "Товар и треки запоминаются: в следующий раз выберешь их из списка.\n\n"
    "<b>Работает весь путь:</b> материалы, карточка, направления, сценарий, озвучка, "
    "музыка, генерация сцен и монтаж готового ролика.\n\n"
    "<b>Команды</b>\n"
    "/new — новый ролик\n"
    "/help — эта инструкция\n\n"
    "<i>Голосом можно везде, где можно текстом.</i>"
)

COLLECT_HELP = (
    "📎 <b>Кидай материалы о товаре</b> — что есть, можно по одному:\n\n"
    "🔗 <b>Ссылка на товар</b> в магазине\n"
    "📸 <b>Фото</b> товара или <b>скриншоты</b> страницы (лучше файлом)\n"
    "✍️ <b>Текст</b> или 🎙 <b>голосовое</b> — что за товар и что хочешь в ролике\n"
    "🎬 <b>Видео</b> файлом или ссылкой на YouTube\n\n"
    "Когда всё закинешь — жми «Готово»."
)


def collect_kb() -> InlineKeyboardMarkup:
    return kb([[("✅ Готово", "m:done")]])


def counts_line(counts: dict[str, int]) -> str:
    parts = [f"{KIND_TITLES.get(k, k)}: {n}" for k, n in sorted(counts.items())]
    return ", ".join(parts) or "пока ничего"


def card_text(card: ProductCard, doubts: list[str] | None = None) -> str:
    cat = categories().get(card.category, {}).get("title", card.category)

    def line(label: str, value: str) -> str:
        return f"<b>{label}:</b> {escape(value)}" if value else f"<b>{label}:</b> <i>—</i>"

    out = [f"📦 <b>{escape(card.name or 'Без названия')}</b>"
           + (f" · {escape(card.brand)}" if card.brand else ""),
           f"<i>{escape(cat)} · язык рекламы: {LANG_TITLES.get(card.language, card.language)}</i>",
           "", line("Что это", card.what_it_is), line("Для кого", card.audience)]
    out.append("<b>Плюсы:</b>" + ("" if card.benefits else " <i>—</i>"))
    out += [f"• {escape(b.text)}" for b in card.benefits]
    if card.specs:
        out.append("<b>Характеристики:</b>")
        out += [f"• {escape(s.text)}" for s in card.specs[:8]]
    out += [line("Цена", card.price),
            line("Акция", (card.offer.text + (f" (до {card.offer.ends_at})"
                                              if card.offer.ends_at else ""))
                 if card.offer else ("нет" if "offer" in card.answered else "")),
            line("Нельзя обещать", "; ".join(card.forbidden_claims)
                 or ("ограничений нет" if "forbidden_claims" in card.answered else "")),
            line("Тон", card.tone), line("Призыв", card.cta)]
    if card.notes:
        out.append(line("Важно", card.notes))
    if doubts:
        out += ["", "⚠️ <b>Проверь:</b>"] + [f"• {escape(d)}" for d in doubts]
    return "\n".join(out)


def review_kb() -> InlineKeyboardMarkup:
    return kb([[("✅ Всё верно", "c:ok")],
               [("✏️ Поправить", "c:edit"), ("📎 Добавить материалы", "c:add")]])


def update_kb() -> InlineKeyboardMarkup:
    return kb([[("✅ Ничего не изменилось", "u:same")],
               [("✏️ Поправить", "c:edit"), ("📎 Добавить материалы", "c:add")]])


# ---------- креатив ----------

ROLE_TITLES = {"hook": "ХУК", "problem": "проблема", "solution": "решение",
               "benefit": "выгода", "demo": "демо", "proof": "доказательство",
               "offer": "оффер", "cta": "призыв"}
SOURCE_TITLES = {"product_photo": "фото товара", "generated_scene": "сцена с товаром",
                 "end_card": "финальная карточка"}


def directions_text(directions: list[Direction]) -> str:
    out = ["🎨 <b>Креативщик предлагает направления</b>\n"]
    for i, d in enumerate(directions, start=1):
        out.append(
            f"<b>{i}. {escape(d.title)}</b> · <i>{escape(d.format)}</i>\n"
            f"🪝 <b>Хук:</b> {escape(d.hook)}\n"
            f"💡 {escape(d.idea)}\n"
            f"🎥 {escape(d.visual_style)}\n"
            f"🎙 Голос: {escape(d.voice_tone)} · 🎵 {escape(d.music_mood)}\n"
            f"✅ {escape(d.why_it_works)}"
            + (f"\n⚠️ {escape(d.risks)}" if d.risks else ""))
    out.append("\nВыбери номер, дай <b>свой вариант</b> или попроси другие.")
    return "\n\n".join(out)


def directions_kb(directions: list) -> InlineKeyboardMarkup:
    rows = [[(f"{i}. {d.title}"[:60], f"cr:d:{i - 1}")]
            for i, d in enumerate(directions, start=1)]
    rows.append([("✏️ Свой вариант", "cr:own"), ("🎲 Другие направления", "cr:more")])
    return kb(rows)


def direction_text(d: Direction) -> str:
    return directions_text([d]).split("\n\n", 1)[1].rsplit("\n\n", 1)[0]


def own_direction_kb() -> InlineKeyboardMarkup:
    return kb([[("✅ Беру", "cr:own_ok")], [("✏️ Сказать иначе", "cr:own"),
                                            ("↩️ К направлениям", "cr:back")]])


def duration_kb() -> InlineKeyboardMarkup:
    return kb([[("15 с", "cr:len:15"), ("20 с", "cr:len:20"), ("30 с", "cr:len:30")]])


def script_text(script: VideoScript) -> list[str]:
    """Сценарий видео — может не влезть в одно сообщение, поэтому список частей."""
    s = script
    head = (f"🎬 <b>Сценарий видео: {escape(s.title)}</b>\n"
            f"Длина {s.total:g} с · сцен {len(s.shots)}")
    blocks, t = [head], 0.0
    for shot in s.shots:
        star = " ⭐ <b>главный кадр</b>" if shot.n == s.key_shot else ""
        text = f"\n🔤 Надпись: «{escape(shot.on_screen_text)}»" if shot.on_screen_text else ""
        ref = f" #{shot.photo_ref}" if shot.photo_ref else ""
        blocks.append(
            f"<b>{shot.n}</b> · {t:g}–{t + shot.duration_sec:g} с · "
            f"{ROLE_TITLES.get(shot.role, shot.role)}{star}\n"
            f"🖼 {escape(shot.visual)}\n"
            f"🎥 {escape(shot.camera)}\n"
            f"📷 {SOURCE_TITLES.get(shot.source, shot.source)}{ref}{text}")
        t = round(t + shot.duration_sec, 2)
    return _pack(blocks)


def _pack(blocks: list[str], limit: int = 3800) -> list[str]:
    parts, cur = [], ""
    for b in blocks:
        if cur and len(cur) + len(b) + 2 > limit:
            parts.append(cur)
            cur = b
        else:
            cur = f"{cur}\n\n{b}" if cur else b
    if cur:
        parts.append(cur)
    return parts


def script_kb() -> InlineKeyboardMarkup:
    return kb([[("✅ Дальше — озвучка", "sc:ok")],
               [("✏️ Поправить", "sc:edit"), ("🔁 Переделать кадр", "sc:redo")],
               [("🎲 Другой вариант", "sc:variant"), ("↩️ Другое направление", "cr:back")]])


def redo_kb(n_shots: int) -> InlineKeyboardMarkup:
    row = [(f"🔁 {i}", f"sc:redo:{i}") for i in range(1, n_shots + 1)]
    return kb([row[:4], row[4:]] if len(row) > 4 else [row])


def voiceover_text(vo: Voiceover, script: VideoScript, chars_per_sec: float) -> list[str]:
    from ..script.validate import char_limit

    by_n = {line.n: line for line in vo.lines}
    blocks = ["🎙 <b>Озвучка под кадры</b>"]
    for shot in script.shots:
        line = by_n.get(shot.n)
        text = line.text.strip() if line else ""
        limit = char_limit(shot.duration_sec, chars_per_sec)
        star = " ⭐" if shot.n == script.key_shot else ""
        body = f"«{escape(text)}»" if text else "<i>пауза — говорит картинка</i>"
        blocks.append(f"<b>{shot.n}</b> · {shot.duration_sec:g} с{star} · "
                      f"{len(text)}/{limit} симв\n{body}")
    return _pack(blocks)


def voiceover_kb() -> InlineKeyboardMarkup:
    return kb([[("✅ Дальше — музыка", "vo:ok")],
               [("✏️ Поправить", "vo:edit"), ("🎲 Другой вариант", "vo:variant")],
               [("↩️ К сценарию", "vo:back")]])


# ---------- музыка ----------

MUSIC_HELP = (
    "🎵 <b>Музыка для ролика</b>\n\n"
    "Пришли свой трек — <b>файлом</b> (mp3, m4a, wav) или <b>ссылкой на YouTube</b>.\n"
    "Я разберу ритм и подберу кусок под сценарий: дроп — на главный кадр, "
    "склейки — в такт. На монтаже музыка будет тише голоса.\n\n"
    "Кусок можно выбрать и самому: секунды, «припев», «вступление»."
)

SEGMENT_HELP = (
    "✂️ <b>Какой кусок трека взять?</b> Напиши или надиктуй:\n"
    "• «0:42–1:02» — точные секунды\n"
    "• «с 30-й секунды на 20 секунд»\n"
    "• «припев» — возьму самое яркое место\n"
    "• «вступление», «концовка», «весь трек»\n\n"
    "Длина куска = длина ролика, от 5 до 30 секунд."
)


def mmss(seconds: float) -> str:
    s = max(0, round(seconds))
    return f"{s // 60}:{s % 60:02d}"


def music_menu_kb(tracks_count: int) -> InlineKeyboardMarkup:
    rows = []
    if tracks_count:
        rows.append([(f"📚 Мои треки ({tracks_count})", "mu:lib")])
    rows.append([("🔇 Без музыки", "mu:none")])
    return kb(rows)


def library_kb(tracks: list[Track]) -> InlineKeyboardMarkup:
    rows = []
    for t in tracks:
        label = f"🎵 {t.title} · {t.bpm:.0f} BPM · {mmss(t.duration)}"
        rows.append([(label[:60], f"mu:t:{t.id}")])
    rows.append([("✏️ Переименовать или удалить", "mu:manage")])
    return kb(rows)


def manage_kb(tracks: list[Track]) -> InlineKeyboardMarkup:
    """Тот же список, но кнопка ведёт в карточку трека, а не в ролик."""
    rows = [[(f"✏️ {t.title}"[:60], f"mu:m:{t.id}")] for t in tracks]
    rows.append([("↩️ К трекам", "mu:lib")])
    return kb(rows)


def track_card(t: Track) -> str:
    return f"🎵 <b>{escape(t.title)}</b>\n{mmss(t.duration)} · {t.bpm:.0f} BPM"


def track_kb(track_id: int) -> InlineKeyboardMarkup:
    return kb([[("✏️ Переименовать", f"mu:r:{track_id}"), ("🗑 Удалить", f"mu:d:{track_id}")],
               [("↩️ К трекам", "mu:lib")]])


def delete_kb(track_id: int) -> InlineKeyboardMarkup:
    return kb([[("🗑 Да, удалить", f"mu:dy:{track_id}"), ("↩️ Оставить", "mu:keep")]])


def rename_prompt(title: str) -> str:
    return (f"✏️ Сейчас трек называется <b>{escape(title)}</b>.\n"
            "Напиши новое название одним сообщением.")


RENAME_CANCEL_KB = kb([[("↩️ Не менять", "mu:rc")]])


def preview_caption(choice: Choice, note: str = "", key_at: float | None = None) -> str:
    """Почему взят именно этот кусок — иначе подбор выглядит случайным."""
    w = choice.window
    m = choice.map
    parts = [f"🎵 Кусок {mmss(w.start)}–{mmss(w.end)} · "
             f"ролик {round(w.end - w.start)} с · {m.bpm:.0f} BPM"]
    if w.drop_at is not None:
        why = f"💥 Дроп на {w.drop_at:.1f} с ролика"
        if key_at is not None:
            why += (" — ровно на главном кадре" if abs(w.drop_at - key_at) <= 1.0
                    else f" (главный кадр на {key_at:.1f} с)")
        parts.append(why)
    else:
        parts.append("💥 Явного дропа тут нет — взял самое энергичное место")
    if m.drops:
        parts.append("📍 Дропы в треке: " + ", ".join(mmss(d) for d in m.drops[:4]))
    if w.loop:
        parts.append("🔁 Трек короче ролика — пойдёт по кругу")
    parts.append("✂️ Не то место? Жми «Свой кусок» или напиши «с 0:42».")
    if note:
        parts.append(f"<i>{escape(note)}</i>")
    return "\n".join(parts)


def preview_kb(track_id: int) -> InlineKeyboardMarkup:
    return kb([[("✅ Беру", "mu:ok")],
               [("🔁 Другой кусок", "mu:next"), ("✂️ Свой кусок", "mu:seg")],
               [("✏️ Название", f"mu:r:{track_id}"), ("🗑 Удалить трек", f"mu:d:{track_id}")],
               [("↩️ Другой трек", "mu:other")]])


START_TITLES = {"product_photo": "с фото товара", "generated_scene": "новая сцена",
                "prev_last_frame": "продолжает предыдущий кадр"}
END_TITLES = {"none": "", "next_start": " → сшивается со следующим кадром",
              "product_photo": " → заканчивается на фото товара"}
TRANSITION_TITLES = {"cut": "резка на движении", "match_cut": "матч-кат",
                     "whip_pan": "смаз камерой", "speed_ramp": "разгон", "morph": "перетекание"}


def final_text(script: VideoScript, vo: Voiceover, choice: Choice | None,
               chars_per_sec: float) -> list[str]:
    """Итог перед генерацией: что снимаем, что говорим, под какую музыку."""
    lines = {line.n: line for line in vo.lines}
    total = script.total
    head = [f"🎬 <b>Итог: {escape(script.title)}</b>",
            f"Длина {total:g} с · сцен {len(script.shots)}"]
    if choice is not None:
        w = choice.window
        drop = f" · дроп на {w.drop_at:.1f} с" if w.drop_at is not None else ""
        head.append(f"🎵 {escape(choice.track.title)} · "
                    f"{mmss(w.start)}–{mmss(w.end)}{drop}")
    else:
        head.append("🔇 Без музыки — только голос")
    blocks = ["\n".join(head)]

    t = 0.0
    for shot in script.shots:
        line = lines.get(shot.n)
        said = line.text.strip() if line else ""
        star = " ⭐" if shot.n == script.key_shot else ""
        text = f"\n🔤 «{escape(shot.on_screen_text)}»" if shot.on_screen_text else ""
        voice = f"\n🎙 «{escape(said)}»" if said else "\n🎙 <i>пауза</i>"
        blocks.append(f"<b>{shot.n}</b> · {t:g}–{t + shot.duration_sec:g} с{star}\n"
                      f"🖼 {escape(shot.visual)}{text}{voice}")
        t = round(t + shot.duration_sec, 2)

    blocks.append("💸 <b>Дальше — платная генерация:</b> каждая сцена станет отдельным "
                  "видеоклипом в 1080p, потом монтаж соберёт готовый ролик. "
                  "Точную сумму покажу перед запуском. Проверь и утверждай.")
    return _pack(blocks)


def done_kb() -> InlineKeyboardMarkup:
    """Ролик готов: переделать целиком (платно) или поменять сценарий."""
    return kb([[("🔁 Сгенерировать заново", "dn:again")],
               [("✏️ Поменять сценарий", "dn:script")]])


def failed_kb() -> InlineKeyboardMarkup:
    """Генерация упала: доделать с места сбоя (готовое не оплачиваем) или заново."""
    return kb([[("▶️ Доделать", "dn:resume")],
               [("🔁 Заново целиком", "dn:again")],
               [("✏️ Поменять сценарий", "dn:script")]])


def final_kb() -> InlineKeyboardMarkup:
    return kb([[("✅ Утвердить и генерировать", "fr:ok")],
               [("✏️ Поменять сценарий", "fr:script"), ("🎙 Поменять озвучку", "fr:voice")],
               [("🎵 Поменять трек", "fr:music")]])


def question(q: Question, n: int, total: int) -> tuple[str, InlineKeyboardMarkup]:
    rows = [[(o[:60], f"q:o:{i}")] for i, o in enumerate(q.options)]
    if q.field == "benefits" and len(q.options) > 1:
        rows.append([("✅ Взять все варианты", "q:all")])
    # Свой вариант — всегда: предложенные варианты лишь подсказка, а не выбор из списка.
    rows.append([("✏️ Свой вариант", "q:own")])
    if q.skippable:
        rows.append([("⏭ Пропустить", "q:skip")])
    hint = "\n\n<i>Жми вариант или просто напиши / надиктуй свой.</i>"
    return f"❓ <b>Вопрос {n} из {total}</b>\n{escape(q.text)}{hint}", kb(rows)
