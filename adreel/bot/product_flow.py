"""Диалог про товар: /new → материалы → разбор → вопросы → карточка → подтверждение."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from html import escape
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, User

from ..ingest.video import MAX_VIDEO_BYTES
from ..product.questions import SKIP, Question
from ..product.service import Added, ProductError, ProductService
from ..script.service import ScriptService
from . import view
from .common import error_text, spawn
from .creative_flow import open_creative
from .states import Flow

log = logging.getLogger("adreel.bot.product")

router = Router(name="product")

_summary_tasks: dict[int, asyncio.Task[Any]] = {}
_summary_msgs: dict[int, int] = {}

MIN_TEXT_MATERIAL = 15


def _lang(user: User | None) -> str:
    code = (user.language_code or "").lower() if user else ""
    return code if code in ("uk", "ru", "en") else "uk"


async def _pid(state: FSMContext) -> int:
    pid = (await state.get_data()).get("pid")
    if not pid:
        raise ProductError("не вижу, с каким товаром работаем — начни с /new")
    return int(pid)


def _user_error(exc: Exception) -> str:
    return error_text(exc, (ProductError,), log)


# ---------- старт ----------

@router.message(Command("new"))
async def cmd_new(message: Message, state: FSMContext, svc: ProductService) -> None:
    await state.clear()
    owner = message.from_user.id if message.from_user else 0
    products = await svc.recent(owner)
    if not products:
        await _start_new(message, state, svc, message.from_user)
        return
    text, markup = view.choose_product(products)
    await state.set_state(Flow.choosing)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "p:new")
async def cb_new(cb: CallbackQuery, state: FSMContext, svc: ProductService) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        await _start_new(cb.message, state, svc, cb.from_user)


async def _start_new(message: Message, state: FSMContext, svc: ProductService,
                     user: User | None) -> None:
    pid = await svc.new_draft(user.id if user else 0, _lang(user))
    await state.set_state(Flow.collecting)
    await state.set_data({"pid": pid})
    await message.answer(view.COLLECT_HELP, reply_markup=view.collect_kb())


@router.callback_query(Flow.choosing, F.data.startswith("p:"))
async def cb_pick(cb: CallbackQuery, state: FSMContext, svc: ProductService) -> None:
    await cb.answer()
    pid = int((cb.data or "p:0").split(":", 1)[1])
    card = await svc.card(pid)
    await state.set_state(Flow.updating)
    await state.set_data({"pid": pid})
    if isinstance(cb.message, Message):
        await cb.message.answer(
            view.card_text(card) + "\n\n<b>Что изменилось с прошлого раза?</b> "
            "Напиши или надиктуй — например «цена теперь 799, акция −10% до 30.09».",
            reply_markup=view.update_kb())


# ---------- материалы ----------

async def _download(bot: Bot, file_id: str, size: int | None = None) -> bytes:
    # Локальный Bot API отдаёт файлы до 2 ГБ, а читаем мы в память целиком.
    if size and size > MAX_VIDEO_BYTES:
        raise ProductError(f"файл больше {MAX_VIDEO_BYTES // 1024 // 1024} МБ — пришли "
                           "покороче или ссылкой")
    try:
        buf = await bot.download(file_id)
    except TelegramBadRequest as exc:
        if "too big" in str(exc).lower():
            raise ProductError("файл больше 20 МБ — Telegram его не отдаёт. Пришли ссылкой "
                               "или подними локальный Bot API (docker compose)") from exc
        raise
    if buf is None:
        raise ProductError("не смог скачать файл из Telegram")
    return buf.read()


async def _after_add(message: Message, bot: Bot, state: FSMContext, svc: ProductService,
                     res: Added) -> None:
    notes = []
    if res.duplicate:
        notes.append(f"🔁 {res.note}")
    elif res.note:
        notes.append(f"ℹ️ {res.note}")
    if res.extra:
        what = "кадров из видео" if res.kind == "video" else "фото со страницы"
        notes.append(f"🖼 Подтянул {res.extra} {what}")
    if notes:
        await message.answer("\n".join(notes))
    _schedule_summary(bot, message.chat.id, await _pid(state), svc)


def _schedule_summary(bot: Bot, chat_id: int, pid: int, svc: ProductService) -> None:
    """Альбом из 10 фото приходит 10 сообщениями — отвечаем одной сводкой, а не десятью."""
    old = _summary_tasks.pop(chat_id, None)
    if old:
        old.cancel()
    _summary_tasks[chat_id] = spawn(_summary(bot, chat_id, pid, svc))


async def _summary(bot: Bot, chat_id: int, pid: int, svc: ProductService) -> None:
    await asyncio.sleep(1.5)
    try:
        counts = await svc.counts(pid)
        prev = _summary_msgs.pop(chat_id, None)
        if prev:
            try:
                await bot.delete_message(chat_id, prev)
            except TelegramBadRequest:
                pass
        msg = await bot.send_message(
            chat_id, f"📥 Есть: {view.counts_line(counts)}.\nКидай ещё или жми «Готово».",
            reply_markup=view.collect_kb())
        _summary_msgs[chat_id] = msg.message_id
    except Exception:
        # Сводка — вежливость, не шаг: материал уже сохранён. Но молча терять нельзя.
        log.exception("не смог прислать сводку материалов")


async def _guarded(message: Message, action: Callable[[], Awaitable[None]]) -> None:
    try:
        await action()
    except Exception as exc:
        await message.answer(_user_error(exc))


@router.message(Flow.collecting, F.photo)
async def got_photo(message: Message, bot: Bot, state: FSMContext, svc: ProductService) -> None:
    async def go() -> None:
        assert message.photo
        data = await _download(bot, message.photo[-1].file_id)
        await _after_add(message, bot, state, svc, await svc.add_photo(await _pid(state), data))
        if message.caption:
            await _add_text_and_links(message, bot, state, svc, message.caption)
    await _guarded(message, go)


@router.message(Flow.collecting, F.video | F.video_note | F.animation)
async def got_video(message: Message, bot: Bot, state: FSMContext, svc: ProductService) -> None:
    async def go() -> None:
        media = message.video or message.video_note or message.animation
        assert media
        note = await message.answer("🎬 Забираю видео…")
        data = await _download(bot, media.file_id, media.file_size)
        res = await svc.add_video(await _pid(state), data)
        await note.delete()
        await _after_add(message, bot, state, svc, res)
    await _guarded(message, go)


@router.message(Flow.collecting, F.document)
async def got_document(message: Message, bot: Bot, state: FSMContext,
                       svc: ProductService) -> None:
    async def go() -> None:
        doc = message.document
        assert doc
        mime = doc.mime_type or ""
        pid = await _pid(state)
        if mime.startswith("image/"):
            res = await svc.add_photo(pid, await _download(bot, doc.file_id, doc.file_size))
        elif mime.startswith("video/"):
            res = await svc.add_video(pid, await _download(bot, doc.file_id, doc.file_size),
                                      mime=mime)
        elif mime.startswith("text/"):
            text = (await _download(bot, doc.file_id, doc.file_size)).decode("utf-8", "replace")
            res = await svc.add_text(pid, text)
        else:
            raise ProductError("такой файл не понимаю: пришли фото, видео или текст")
        await _after_add(message, bot, state, svc, res)
    await _guarded(message, go)


@router.message(Flow.collecting, F.voice | F.audio)
async def got_voice(message: Message, bot: Bot, state: FSMContext, svc: ProductService) -> None:
    async def go() -> None:
        audio = message.voice or message.audio
        assert audio
        data = await _download(bot, audio.file_id)
        card = await svc.card(await _pid(state))
        text = await svc.add_voice(await _pid(state), data, "voice.ogg",
                                   float(audio.duration or 0), card.language)
        await message.answer(f"🎙 Записал: <i>{escape(text[:500])}</i>")
        _schedule_summary(bot, message.chat.id, await _pid(state), svc)
    await _guarded(message, go)


@router.message(Flow.collecting, F.text)
async def got_text(message: Message, bot: Bot, state: FSMContext, svc: ProductService) -> None:
    await _guarded(message, lambda: _add_text_and_links(message, bot, state, svc,
                                                        message.text or ""))


async def _add_text_and_links(message: Message, bot: Bot, state: FSMContext,
                              svc: ProductService, text: str) -> None:
    from ..ingest.pages import find_urls, strip_urls

    pid = await _pid(state)
    for url in find_urls(text):
        note = await message.answer(f"🔗 Открываю {escape(url[:80])}…")
        try:
            res = await svc.add_link(pid, url)
        finally:
            await note.delete()
        await _after_add(message, bot, state, svc, res)
    rest = strip_urls(text)
    if len(rest) >= MIN_TEXT_MATERIAL:
        await _after_add(message, bot, state, svc, await svc.add_text(pid, rest))


@router.callback_query(Flow.collecting, F.data == "m:done")
async def cb_done(cb: CallbackQuery, bot: Bot, state: FSMContext, svc: ProductService) -> None:
    # Первой строкой, до любого await: апдейты идут параллельно, и второй тап иначе
    # прошёл бы фильтр состояния — два платных разбора LLM.
    await state.set_state(Flow.busy)
    pid = int((await state.get_data()).get("pid") or 0)
    if not pid or not await svc.counts(pid):
        await state.set_state(Flow.collecting)
        await cb.answer("Сначала закинь хоть что-нибудь о товаре", show_alert=True)
        return
    await cb.answer()
    if not isinstance(cb.message, Message):
        await state.set_state(Flow.collecting)
        return
    status = await cb.message.answer("⏳ Разбираю материалы…")
    spawn(_run_analysis(cb.message.chat.id, bot, state, svc, pid, status))


async def _run_analysis(chat_id: int, bot: Bot, state: FSMContext, svc: ProductService,
                        pid: int, status: Message) -> None:
    async def progress(text: str) -> None:
        try:
            await status.edit_text(text)
        except TelegramBadRequest:
            pass

    try:
        res = await svc.analyze(pid, progress)
    except Exception as exc:
        await state.set_state(Flow.collecting)
        await bot.send_message(chat_id, _user_error(exc) + "\n\nМатериалы на месте — "
                               "можно добавить ещё и снова нажать «Готово».",
                               reply_markup=view.collect_kb())
        return
    await progress(f"✅ Разобрал. Потрачено на разбор: ${await svc.spent_usd(pid):.3f}")
    await state.update_data(questions=[asdict(q) for q in res.questions], qi=0,
                            doubts=res.doubts)
    if res.questions:
        await bot.send_message(chat_id, view.card_text(res.card) +
                               f"\n\nОсталось уточнить {len(res.questions)} — поехали.")
        await state.set_state(Flow.asking)
        await _ask(chat_id, bot, state)
    else:
        await _show_review(chat_id, bot, state, svc)


# ---------- анкета ----------

async def _current_question(state: FSMContext) -> tuple[Question | None, int, int]:
    data = await state.get_data()
    qs = [Question(**q) for q in data.get("questions", [])]
    i = int(data.get("qi", 0))
    return (qs[i] if i < len(qs) else None), i, len(qs)


async def _ask(chat_id: int, bot: Bot, state: FSMContext) -> None:
    q, i, total = await _current_question(state)
    if q is None:
        return
    text, markup = view.question(q, i + 1, total)
    await bot.send_message(chat_id, text, reply_markup=markup)


async def _answer(chat_id: int, bot: Bot, state: FSMContext, svc: ProductService,
                  value: str) -> None:
    q, i, total = await _current_question(state)
    if q is None:
        await _show_review(chat_id, bot, state, svc)
        return
    await svc.answer(await _pid(state), q.field, value)
    await state.update_data(qi=i + 1)
    if i + 1 < total:
        await _ask(chat_id, bot, state)
    else:
        await _show_review(chat_id, bot, state, svc)


@router.message(Flow.asking, F.text)
async def ask_text(message: Message, bot: Bot, state: FSMContext, svc: ProductService) -> None:
    await _guarded(message, lambda: _answer(message.chat.id, bot, state, svc,
                                            message.text or ""))


@router.message(Flow.asking, F.voice | F.audio)
async def ask_voice(message: Message, bot: Bot, state: FSMContext, svc: ProductService) -> None:
    async def go() -> None:
        text = await _transcribe(message, bot, state, svc)
        await _answer(message.chat.id, bot, state, svc, text)
    await _guarded(message, go)


@router.callback_query(Flow.asking, F.data.startswith("q:"))
async def ask_button(cb: CallbackQuery, bot: Bot, state: FSMContext,
                     svc: ProductService) -> None:
    await cb.answer()
    q, _, _ = await _current_question(state)
    if q is None or not isinstance(cb.message, Message):
        return
    if cb.data == "q:own":
        # Состояние не меняем: следующий текст или голосовое и есть ответ на этот вопрос.
        await cb.message.answer("✏️ Напиши или надиктуй свой вариант — следующим сообщением.")
        return
    data = cb.data or ""
    if data == "q:skip":
        value = SKIP
    elif data == "q:all":
        value = "\n".join(q.options)
    else:
        idx = int(data.rsplit(":", 1)[1])
        if idx >= len(q.options):
            return
        value = q.options[idx]
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(f"👉 {escape(value if value != SKIP else 'пропустил')}")
    await _answer(cb.message.chat.id, bot, state, svc, value)


# ---------- карточка ----------

async def _show_review(chat_id: int, bot: Bot, state: FSMContext, svc: ProductService) -> None:
    data = await state.get_data()
    card = await svc.card(await _pid(state))
    await state.set_state(Flow.reviewing)
    await bot.send_message(chat_id, view.card_text(card, data.get("doubts") or None) +
                           "\n\nВсё верно?", reply_markup=view.review_kb())


@router.callback_query(Flow.reviewing, F.data == "c:ok")
@router.callback_query(Flow.updating, F.data == "u:same")
async def cb_confirm(cb: CallbackQuery, bot: Bot, state: FSMContext, svc: ProductService,
                     scripts: ScriptService) -> None:
    await cb.answer()
    if not isinstance(cb.message, Message):
        return
    pid = await _pid(state)
    try:
        card = await svc.confirm(pid)
    except ProductError as exc:
        await cb.message.answer(_user_error(exc))
        return
    await cb.message.edit_reply_markup(reply_markup=None)
    await state.set_state(Flow.ready)
    await state.update_data(questions=[], qi=0, doubts=[])
    await cb.message.answer(
        f"✅ Карточка <b>{escape(card.name)}</b> сохранена — в следующий раз предложу её сразу.")
    await open_creative(cb.message.chat.id, bot, state, scripts, pid)


@router.callback_query(F.data == "c:edit")
async def cb_edit(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not (await state.get_data()).get("pid") or not isinstance(cb.message, Message):
        return
    await state.set_state(Flow.editing)
    await cb.message.answer("✏️ Напиши или надиктуй, что поправить. Например: "
                            "«аудитория — молодые мамы, убери пункт про гарантию».")


@router.callback_query(F.data == "c:add")
async def cb_add(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not (await state.get_data()).get("pid") or not isinstance(cb.message, Message):
        return
    await state.set_state(Flow.collecting)
    await cb.message.answer(view.COLLECT_HELP, reply_markup=view.collect_kb())


@router.message(Flow.editing, F.text)
@router.message(Flow.updating, F.text)
async def edit_text(message: Message, bot: Bot, state: FSMContext, svc: ProductService) -> None:
    await _guarded(message, lambda: _start_edit(message, bot, state, svc, message.text or ""))


@router.message(Flow.editing, F.voice | F.audio)
@router.message(Flow.updating, F.voice | F.audio)
async def edit_voice(message: Message, bot: Bot, state: FSMContext,
                     svc: ProductService) -> None:
    async def go() -> None:
        await _start_edit(message, bot, state, svc, await _transcribe(message, bot, state, svc))
    await _guarded(message, go)


async def _start_edit(message: Message, bot: Bot, state: FSMContext, svc: ProductService,
                      instruction: str) -> None:
    pid = await _pid(state)
    back = await state.get_state()
    await state.set_state(Flow.busy)
    status = await message.answer("✏️ Правлю карточку…")

    async def run() -> None:
        try:
            await svc.edit(pid, instruction)
        except Exception as exc:
            await state.set_state(back)
            await message.answer(_user_error(exc))
            return
        try:
            await status.delete()
        except TelegramBadRequest:
            pass
        await state.update_data(doubts=[])
        await _show_review(message.chat.id, bot, state, svc)

    spawn(run())


async def _transcribe(message: Message, bot: Bot, state: FSMContext,
                      svc: ProductService) -> str:
    audio = message.voice or message.audio
    assert audio
    pid = await _pid(state)
    card = await svc.card(pid)
    text = await svc.transcribe(pid, await _download(bot, audio.file_id), "voice.ogg",
                                float(audio.duration or 0), card.language)
    await message.answer(f"🎙 <i>{escape(text[:500])}</i>")
    return text


# ---------- прочее ----------

@router.message(Flow.busy)
async def busy_message(message: Message) -> None:
    await message.answer("⏳ Ещё работаю — секунду.")


@router.callback_query(Flow.busy)
async def busy_button(cb: CallbackQuery) -> None:
    await cb.answer("⏳ Ещё работаю — секунду.")


@router.callback_query()
async def stale_button(cb: CallbackQuery) -> None:
    await cb.answer("Эта кнопка из прошлого шага. /new — начать заново.", show_alert=True)


@router.message()
async def fallback(message: Message) -> None:
    await message.answer("Чтобы начать, нажми /new.")
