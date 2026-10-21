"""Креатив ролика: направления → (свой вариант) → длина → сценарий видео → озвучка → музыка."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from html import escape
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from ..gen.pipeline import GenerationError, GenerationService
from ..llm.base import LLMError
from ..music.service import MusicService
from ..product.service import ProductError, ProductService
from ..script.creative import ScriptError
from ..script.schema import Direction
from ..script.service import ScriptService
from . import view
from .common import error_text, spawn
from .music_flow import open_music
from .states import Flow

log = logging.getLogger("adreel.bot.creative")

router = Router(name="creative")

NOT_COMMAND = ~F.text.startswith("/")


def _err(exc: Exception) -> str:
    return error_text(exc, (ScriptError, ProductError, LLMError, GenerationError), log)


async def _project(state: FSMContext) -> int:
    pid = (await state.get_data()).get("project_id")
    if not pid:
        raise ScriptError("не вижу, с каким роликом работаем — начни с /new")
    return int(pid)


async def _work(chat_id: int, bot: Bot, state: FSMContext, status_text: str,
                job: Callable[[], Awaitable[None]], back: State) -> None:
    """Долгий вызов агента в фоне: бот отвечает, пока агент думает.
    Упало — возвращаем на шаг back и показываем причину."""
    await state.set_state(Flow.creative_busy)
    status = await bot.send_message(chat_id, status_text)

    async def run() -> None:
        try:
            await job()
        except Exception as exc:
            await state.set_state(back)
            await bot.send_message(chat_id, _err(exc))
        finally:
            try:
                await status.delete()
            except TelegramBadRequest:
                pass

    spawn(run())


async def _send_parts(bot: Bot, chat_id: int, parts: list[str], markup: Any) -> None:
    for i, part in enumerate(parts):
        await bot.send_message(chat_id, part,
                               reply_markup=markup if i == len(parts) - 1 else None)


async def _voice_or_text(message: Message, bot: Bot, state: FSMContext,
                         svc: ProductService) -> str:
    voice = message.voice or message.audio
    if voice is None:
        return (message.text or "").strip()
    buf = await bot.download(voice.file_id)
    if buf is None:
        raise ScriptError("не смог скачать голосовое")
    pid = int((await state.get_data()).get("pid") or 0)
    text = await svc.transcribe(pid, buf.read(), "voice.ogg", float(voice.duration or 0))
    await message.answer(f"🎙 <i>{escape(text)}</i>")
    return text


# ---------- вход ----------

async def open_creative(chat_id: int, bot: Bot, state: FSMContext, scripts: ScriptService,
                        product_id: int) -> None:
    """После подтверждения карточки: новый ролик и направления от креативщика."""
    project_id = await scripts.start_project(product_id)
    await state.update_data(project_id=project_id, project_pid=product_id, directions=[])
    await _propose(chat_id, bot, state, scripts)


async def _propose(chat_id: int, bot: Bot, state: FSMContext, scripts: ScriptService,
                   wishes: str = "", more: bool = False) -> None:
    project_id = await _project(state)
    old = [Direction.model_validate(d) for d in (await state.get_data()).get("directions", [])]

    async def job() -> None:
        res = await scripts.directions(project_id, wishes=wishes, avoid=old if more else None)
        dirs = res.directions
        await state.update_data(directions=[d.model_dump() for d in dirs])
        await state.set_state(Flow.directions)
        await _send_parts(bot, chat_id, view._pack(view.directions_text(dirs).split("\n\n")),
                          view.directions_kb(dirs))

    await _work(chat_id, bot, state, "🎨 Креативщик изучает материалы и придумывает "
                                     "направления…", job, Flow.directions)


# ---------- направления ----------

@router.callback_query(Flow.directions, F.data.startswith("cr:d:"))
async def cb_pick_direction(cb: CallbackQuery, state: FSMContext, scripts: ScriptService) -> None:
    await cb.answer()
    if not isinstance(cb.message, Message):
        return
    idx = int((cb.data or "cr:d:0").rsplit(":", 1)[1])
    dirs = (await state.get_data()).get("directions", [])
    if idx >= len(dirs):
        return
    d = Direction.model_validate(dirs[idx])
    await scripts.choose_direction(await _project(state), d)
    await cb.message.edit_reply_markup(reply_markup=None)
    await state.set_state(Flow.duration)
    await cb.message.answer(f"👉 Направление: <b>{escape(d.title)}</b>\n\n"
                            "⏱ Какой длины ролик?", reply_markup=view.duration_kb())


@router.callback_query(Flow.directions, F.data == "cr:more")
async def cb_more(cb: CallbackQuery, bot: Bot, state: FSMContext, scripts: ScriptService) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        await _propose(cb.message.chat.id, bot, state, scripts, more=True)


@router.callback_query(Flow.directions, F.data == "cr:own")
@router.callback_query(Flow.direction_check, F.data == "cr:own")
async def cb_own(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.set_state(Flow.direction_own)
    if isinstance(cb.message, Message):
        await cb.message.answer(
            "✏️ Опиши своими словами или голосом, каким видишь ролик. Например: «утро, "
            "девушка в машине в пробке, кофе остыл — а в нашей кружке горячий, лёгкий юмор». "
            "Креативщик оформит это в направление и достроит недостающее.")


@router.message(Flow.direction_own, F.voice | F.audio | (F.text & NOT_COMMAND))
@router.message(Flow.directions, F.voice | F.audio | (F.text & NOT_COMMAND))
async def got_own(message: Message, bot: Bot, state: FSMContext, scripts: ScriptService,
                  svc: ProductService) -> None:
    try:
        idea = await _voice_or_text(message, bot, state, svc)
        project_id = await _project(state)
    except Exception as exc:
        await message.answer(_err(exc))
        return
    chat_id = message.chat.id

    async def job() -> None:
        d = await scripts.own_direction(project_id, idea)
        await state.update_data(own_direction=d.model_dump())
        await state.set_state(Flow.direction_check)
        await bot.send_message(chat_id, "🎨 <b>Твоё направление</b>\n\n" + view.direction_text(d),
                               reply_markup=view.own_direction_kb())

    await _work(chat_id, bot, state, "🎨 Оформляю твою идею…", job, Flow.direction_own)


@router.callback_query(Flow.direction_check, F.data == "cr:own_ok")
async def cb_own_ok(cb: CallbackQuery, state: FSMContext, scripts: ScriptService) -> None:
    await cb.answer()
    raw = (await state.get_data()).get("own_direction")
    if not raw or not isinstance(cb.message, Message):
        return
    d = Direction.model_validate(raw)
    await scripts.choose_direction(await _project(state), d)
    await cb.message.edit_reply_markup(reply_markup=None)
    await state.set_state(Flow.duration)
    await cb.message.answer("⏱ Какой длины ролик?", reply_markup=view.duration_kb())


@router.callback_query(F.data == "cr:back")
async def cb_back_to_directions(cb: CallbackQuery, bot: Bot, state: FSMContext,
                                scripts: ScriptService) -> None:
    await cb.answer()
    if not isinstance(cb.message, Message):
        return
    dirs = [Direction.model_validate(d) for d in (await state.get_data()).get("directions", [])]
    if not dirs:
        await _propose(cb.message.chat.id, bot, state, scripts)
        return
    await state.set_state(Flow.directions)
    await _send_parts(bot, cb.message.chat.id,
                      view._pack(view.directions_text(dirs).split("\n\n")),
                      view.directions_kb(dirs))


# ---------- сценарий видео ----------

@router.callback_query(Flow.duration, F.data.startswith("cr:len:"))
async def cb_duration(cb: CallbackQuery, bot: Bot, state: FSMContext,
                      scripts: ScriptService) -> None:
    await cb.answer()
    if not isinstance(cb.message, Message):
        return
    seconds = int((cb.data or "cr:len:20").rsplit(":", 1)[1])
    project_id = await _project(state)
    await scripts.set_duration(project_id, seconds)
    await cb.message.edit_reply_markup(reply_markup=None)
    await _script_job(cb.message.chat.id, bot, state, scripts, "new",
                      f"🎬 Режиссёр пишет сценарий на {seconds} с…", back=Flow.duration)


async def _script_job(chat_id: int, bot: Bot, state: FSMContext, scripts: ScriptService,
                      kind: str, status: str, *, text: str = "", shot: int | None = None,
                      back: State = Flow.script_review) -> None:
    project_id = await _project(state)

    async def job() -> None:
        script = await scripts.make_script(project_id, kind, text=text, shot=shot)
        await state.set_state(Flow.script_review)
        await _send_parts(bot, chat_id, view.script_text(script), view.script_kb())

    await _work(chat_id, bot, state, status, job, back)


@router.callback_query(Flow.script_review, F.data == "sc:variant")
async def cb_script_variant(cb: CallbackQuery, bot: Bot, state: FSMContext,
                            scripts: ScriptService) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        await _script_job(cb.message.chat.id, bot, state, scripts, "variant",
                          "🎬 Пишу другой вариант сценария…")


@router.callback_query(Flow.script_review, F.data == "sc:redo")
async def cb_script_redo_menu(cb: CallbackQuery, state: FSMContext,
                              scripts: ScriptService) -> None:
    await cb.answer()
    c = await scripts.get(await _project(state))
    if c.script and isinstance(cb.message, Message):
        await cb.message.answer("🔁 Какую сцену переделать?",
                                reply_markup=view.redo_kb(len(c.script.shots)))


@router.callback_query(Flow.script_review, F.data.startswith("sc:redo:"))
async def cb_script_redo(cb: CallbackQuery, bot: Bot, state: FSMContext,
                         scripts: ScriptService) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        n = int((cb.data or "sc:redo:1").rsplit(":", 1)[1])
        await cb.message.edit_reply_markup(reply_markup=None)
        await _script_job(cb.message.chat.id, bot, state, scripts, "redo",
                          f"🔁 Переделываю сцену {n}…", shot=n)


@router.callback_query(Flow.script_review, F.data == "sc:edit")
async def cb_script_edit(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.set_state(Flow.script_edit)
    if isinstance(cb.message, Message):
        await cb.message.answer("✏️ Напиши или надиктуй, что поменять. Например: «в кадре 2 "
                                "вместо офиса — машина», «хук динамичнее», «главный кадр — 4».")


@router.message(Flow.script_edit, F.voice | F.audio | (F.text & NOT_COMMAND))
@router.message(Flow.script_review, F.voice | F.audio | (F.text & NOT_COMMAND))
async def got_script_edit(message: Message, bot: Bot, state: FSMContext,
                          scripts: ScriptService, svc: ProductService) -> None:
    try:
        text = await _voice_or_text(message, bot, state, svc)
    except Exception as exc:
        await message.answer(_err(exc))
        return
    await _script_job(message.chat.id, bot, state, scripts, "edit", "✏️ Правлю сценарий…",
                      text=text, back=Flow.script_edit)


# ---------- озвучка ----------

async def _voice_job(chat_id: int, bot: Bot, state: FSMContext, scripts: ScriptService,
                     kind: str, status: str, *, text: str = "",
                     back: State = Flow.voice_review) -> None:
    project_id = await _project(state)

    async def job() -> None:
        vo = await scripts.make_voiceover(project_id, kind, text=text)
        c = await scripts.get(project_id)
        await state.set_state(Flow.voice_review)
        assert c.script is not None
        await _send_parts(bot, chat_id,
                          view.voiceover_text(vo, c.script, scripts.chars_per_sec()),
                          view.voiceover_kb())

    await _work(chat_id, bot, state, status, job, back)


@router.callback_query(Flow.script_review, F.data == "sc:ok")
async def cb_script_ok(cb: CallbackQuery, bot: Bot, state: FSMContext,
                       scripts: ScriptService) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        await cb.message.edit_reply_markup(reply_markup=None)
        await _voice_job(cb.message.chat.id, bot, state, scripts, "new",
                         "🎙 Копирайтер пишет озвучку под кадры…", back=Flow.script_review)


@router.callback_query(Flow.voice_review, F.data == "vo:variant")
async def cb_voice_variant(cb: CallbackQuery, bot: Bot, state: FSMContext,
                           scripts: ScriptService) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        await _voice_job(cb.message.chat.id, bot, state, scripts, "variant",
                         "🎙 Пишу другой вариант озвучки…")


@router.callback_query(Flow.voice_review, F.data == "vo:edit")
async def cb_voice_edit(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.set_state(Flow.voice_edit)
    if isinstance(cb.message, Message):
        await cb.message.answer("✏️ Напиши или надиктуй, что поменять в озвучке. Например: "
                                "«хук вопросом», «в кадре 3 — про доставку», «проще и короче».")


@router.message(Flow.voice_edit, F.voice | F.audio | (F.text & NOT_COMMAND))
@router.message(Flow.voice_review, F.voice | F.audio | (F.text & NOT_COMMAND))
async def got_voice_edit(message: Message, bot: Bot, state: FSMContext,
                         scripts: ScriptService, svc: ProductService) -> None:
    try:
        text = await _voice_or_text(message, bot, state, svc)
    except Exception as exc:
        await message.answer(_err(exc))
        return
    await _voice_job(message.chat.id, bot, state, scripts, "edit", "✏️ Правлю озвучку…",
                     text=text, back=Flow.voice_edit)


@router.callback_query(Flow.voice_review, F.data == "vo:back")
async def cb_voice_back(cb: CallbackQuery, bot: Bot, state: FSMContext,
                        scripts: ScriptService) -> None:
    await cb.answer()
    c = await scripts.get(await _project(state))
    if c.script is None or not isinstance(cb.message, Message):
        return
    await state.set_state(Flow.script_review)
    await _send_parts(bot, cb.message.chat.id, view.script_text(c.script), view.script_kb())


@router.callback_query(Flow.voice_review, F.data == "vo:ok")
async def cb_voice_ok(cb: CallbackQuery, bot: Bot, state: FSMContext,
                      music: MusicService) -> None:
    await cb.answer()
    if not isinstance(cb.message, Message):
        return
    await cb.message.edit_reply_markup(reply_markup=None)
    data = await state.get_data()
    await cb.message.answer("✅ Сценарий и озвучка готовы.")
    await open_music(cb.message.chat.id, bot, state, music, cb.from_user.id,
                     int(data.get("pid") or 0))


# ---------- итог перед генерацией ----------

#: ScriptService кладётся сюда при старте (app.py): итог открывается из музыки,
#: куда этот сервис в обработчик не приходит
_scripts_holder: dict[str, ScriptService] = {}


def set_scripts(scripts: ScriptService) -> None:
    _scripts_holder["svc"] = scripts


def _scripts() -> ScriptService:
    scripts = _scripts_holder.get("svc")
    if scripts is None:
        raise ScriptError("сервис сценария не готов — перезапусти бота")
    return scripts


async def open_final(chat_id: int, bot: Bot, state: FSMContext, music: MusicService) -> None:
    """Всё вместе перед генерацией: сценарий, озвучка, трек — и утверждение."""
    try:
        scripts = _scripts()
        project_id = await _project(state)
        c = await scripts.get(project_id)
        choice = await music.current(project_id)
    except Exception as exc:
        await bot.send_message(chat_id, _err(exc))
        return
    if c.script is None or c.voiceover is None:
        await bot.send_message(chat_id, "⚠️ Нет сценария или озвучки — начни с /new")
        return
    await state.set_state(Flow.final_review)
    parts = view.final_text(c.script, c.voiceover, choice, scripts.chars_per_sec())
    await _send_parts(bot, chat_id, parts, view.final_kb())


@router.callback_query(Flow.final_review, F.data == "fr:ok")
async def cb_final_ok(cb: CallbackQuery, bot: Bot, state: FSMContext,
                      gen: GenerationService) -> None:
    # Первой строкой, до любого await: aiogram обрабатывает апдейты параллельно, и второй
    # тап по кнопке иначе успел бы пройти фильтр состояния — две платные генерации.
    await state.set_state(Flow.creative_busy)
    await cb.answer()
    if not isinstance(cb.message, Message):
        await state.set_state(Flow.final_review)
        return
    await cb.message.edit_reply_markup(reply_markup=None)
    chat_id = cb.message.chat.id

    async def job() -> None:
        project_id = await _project(state)
        # Оператор работает молча: владельцу важен результат, а не промпты под модель.
        await _scripts().make_shot_plans(project_id)
        await gen.drop(project_id)            # старые клипы под прошлый сценарий не годятся
        await run_generation(chat_id, bot, state, gen, project_id)

    await _work(chat_id, bot, state, "⏳ Готовлю задания для генерации…", job,
                Flow.final_review)


async def run_generation(chat_id: int, bot: Bot, state: FSMContext, gen: GenerationService,
                         project_id: int) -> None:
    """Всё разом: сцены генерируются, монтаж склеивает, владельцу уходит готовый ролик.
    Готовые сцены не переснимаются — «заново» сначала делает gen.drop()."""
    await state.set_state(Flow.generating)
    estimate = await gen.estimate(project_id)
    status = await bot.send_message(
        chat_id, f"🎬 Делаю ролик: {estimate.text}." + 
                 "\nОбычно это 3–5 минут, пришлю готовое видео.")

    async def progress(text: str) -> None:
        try:
            await status.edit_text(f"🎬 {escape(text)}")
        except TelegramBadRequest:
            pass

    async def job() -> None:
        try:
            await gen.generate(project_id, progress=progress)
            video = await gen.assemble(project_id, progress)
        finally:
            try:
                await status.delete()
            except TelegramBadRequest:
                pass
        c = await _scripts().get(project_id)
        spent = await _scripts().products.spent_usd(c.project.product_id)
        await state.set_state(Flow.done)
        await bot.send_video(
            chat_id, BufferedInputFile(video, filename="adreel.mp4"),
            caption=f"✅ Готовый ролик · {c.project.duration_sec} с · "
                    f"потрачено на товар ${spent:.2f}",
            reply_markup=view.done_kb())

    spawn(_guard_generation(chat_id, bot, state, job))


async def _guard_generation(chat_id: int, bot: Bot, state: FSMContext,
                            job: Callable[[], Awaitable[None]]) -> None:
    try:
        await job()
    except Exception as exc:
        await state.set_state(Flow.done)
        await bot.send_message(
            chat_id, _err(exc) + "\n\nГотовые сцены сохранены. «Доделать» продолжит "
                                 "с места сбоя и не заплатит за них второй раз.",
            reply_markup=view.failed_kb())


@router.callback_query(Flow.done, F.data == "dn:resume")
async def cb_resume(cb: CallbackQuery, bot: Bot, state: FSMContext,
                    gen: GenerationService) -> None:
    await state.set_state(Flow.generating)          # до await — против двойного тапа
    await cb.answer()
    if not isinstance(cb.message, Message):
        await state.set_state(Flow.done)
        return
    await _start_generation(cb.message, bot, state, gen, fresh=False)


@router.callback_query(Flow.done, F.data == "dn:again")
async def cb_again(cb: CallbackQuery, bot: Bot, state: FSMContext,
                   gen: GenerationService) -> None:
    await state.set_state(Flow.generating)          # до await — против двойного тапа
    await cb.answer()
    if not isinstance(cb.message, Message):
        await state.set_state(Flow.done)
        return
    await _start_generation(cb.message, bot, state, gen, fresh=True)


async def _start_generation(message: Message, bot: Bot, state: FSMContext,
                            gen: GenerationService, *, fresh: bool) -> None:
    """fresh — заново целиком, с новым сидом (иначе выйдет почти то же самое).
    Иначе доделываем: готовые сцены и оплаченные кадры остаются."""
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    try:
        project_id = await _project(state)
        if fresh:
            await gen.drop(project_id, reseed=True)
        await run_generation(message.chat.id, bot, state, gen, project_id)
    except Exception as exc:
        await state.set_state(Flow.done)
        await bot.send_message(message.chat.id, _err(exc), reply_markup=view.failed_kb())


@router.callback_query(Flow.done, F.data == "dn:script")
async def cb_done_script(cb: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    await cb.answer()
    if not isinstance(cb.message, Message):
        return
    c = await _scripts().get(await _project(state))
    if c.script is None:
        return
    await state.set_state(Flow.script_review)
    await _send_parts(bot, cb.message.chat.id, view.script_text(c.script), view.script_kb())


@router.message(Flow.generating)
async def generating(message: Message) -> None:
    await message.answer("⏳ Генерация идёт — это пара минут. Подожди.")


@router.callback_query(Flow.generating)
async def generating_cb(cb: CallbackQuery) -> None:
    await cb.answer("⏳ Генерация идёт — подожди.")


@router.callback_query(Flow.final_review, F.data == "fr:script")
async def cb_final_script(cb: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    await cb.answer()
    if not isinstance(cb.message, Message):
        return
    c = await _scripts().get(await _project(state))
    if c.script is None:
        return
    await state.set_state(Flow.script_review)
    await _send_parts(bot, cb.message.chat.id, view.script_text(c.script), view.script_kb())


@router.callback_query(Flow.final_review, F.data == "fr:voice")
async def cb_final_voice(cb: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    await cb.answer()
    if not isinstance(cb.message, Message):
        return
    scripts = _scripts()
    c = await scripts.get(await _project(state))
    if c.script is None or c.voiceover is None:
        return
    await state.set_state(Flow.voice_review)
    await _send_parts(bot, cb.message.chat.id,
                      view.voiceover_text(c.voiceover, c.script, scripts.chars_per_sec()),
                      view.voiceover_kb())


@router.callback_query(Flow.final_review, F.data == "fr:music")
async def cb_final_music(cb: CallbackQuery, bot: Bot, state: FSMContext,
                         music: MusicService) -> None:
    await cb.answer()
    if not isinstance(cb.message, Message):
        return
    data = await state.get_data()
    await open_music(cb.message.chat.id, bot, state, music, cb.from_user.id,
                     int(data.get("pid") or 0))


# ---------- пока агент думает ----------

@router.message(Flow.creative_busy)
async def busy(message: Message) -> None:
    await message.answer("⏳ Агент ещё думает — секунду.")


@router.callback_query(Flow.creative_busy)
async def busy_cb(cb: CallbackQuery) -> None:
    await cb.answer("⏳ Агент ещё думает — секунду.")


@router.message(Flow.direction_check, NOT_COMMAND)
@router.message(Flow.duration, NOT_COMMAND)
async def use_buttons(message: Message) -> None:
    await message.answer("👆 Выбери кнопкой под сообщением выше.")
