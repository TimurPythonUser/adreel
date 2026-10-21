"""Музыка ролика: свой трек → кусок (подобранный или свой) → превью → беру."""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from ..db.models import Track
from ..ingest.pages import find_urls
from ..music.analyze import MusicError
from ..music.service import Choice, MusicService, PendingTrack
from ..product.service import ProductError, ProductService
from . import view
from .common import error_text, spawn
from .states import Flow

log = logging.getLogger("adreel.bot.music")

router = Router(name="music")

#: чтобы «/new» и «/help» в музыкальных шагах доходили до своих обработчиков
NOT_COMMAND = ~F.text.startswith("/")

#: где видны кнопки библиотеки и превью — там же переименование и удаление трека
LIBRARY = StateFilter(Flow.music, Flow.music_preview)

#: локальный Bot API отдаёт файлы до 2 ГБ, а трек читаем в память целиком
MAX_TRACK_BYTES = 100 * 1024 * 1024


def _err(exc: Exception) -> str:
    return error_text(exc, (MusicError, ProductError), log)


async def _ids(state: FSMContext) -> tuple[int, int]:
    data = await state.get_data()
    if not data.get("project_id") or not data.get("pid"):
        raise MusicError("не вижу, с каким роликом работаем — начни с /new")
    return int(data["project_id"]), int(data["pid"])


# ---------- вход в шаг ----------

async def open_music(chat_id: int, bot: Bot, state: FSMContext, music: MusicService,
                     owner_id: int, pid: int) -> None:
    """Вызывается после подтверждения карточки: заводим ролик и просим трек."""
    data = await state.get_data()
    project_id = data.get("project_id")
    if not project_id or int(data.get("project_pid", 0)) != pid:
        project_id = await music.start_project(pid)
    await state.update_data(project_id=project_id, project_pid=pid)
    await _menu(chat_id, bot, state, music, owner_id)


async def _menu(chat_id: int, bot: Bot, state: FSMContext, music: MusicService,
                owner_id: int) -> None:
    await state.set_state(Flow.music)
    n = await music.count_tracks(owner_id)
    await bot.send_message(chat_id, view.MUSIC_HELP, reply_markup=view.music_menu_kb(n))


# ---------- трек ----------

@router.message(Flow.music, F.audio | F.document | (F.text & NOT_COMMAND))
async def got_track(message: Message, bot: Bot, state: FSMContext, music: MusicService) -> None:
    owner = message.from_user.id if message.from_user else 0
    try:
        if message.audio:
            a = message.audio
            title = " — ".join(x for x in (a.performer, a.title) if x)
            job = _from_file(bot, music, owner, a.file_id, a.file_name or "track.mp3", title,
                             a.mime_type or "", a.file_size)
        elif message.document and (message.document.mime_type or "").startswith(("audio/",
                                                                                "video/")):
            d = message.document
            job = _from_file(bot, music, owner, d.file_id, d.file_name or "track.mp3", "",
                             d.mime_type or "", d.file_size)
        elif message.text and (urls := find_urls(message.text)):
            job = music.prepare_link(owner, urls[0])
        else:
            await message.answer("🎵 Пришли трек файлом (mp3, m4a, wav) или ссылкой на YouTube.")
            return
    except Exception as exc:
        await message.answer(_err(exc))
        return

    await state.set_state(Flow.music_busy)
    status = await message.answer("🎧 Слушаю трек: темп, биты, где дроп…")
    spawn(_after_prepare(message.chat.id, owner, bot, state, music, job, status))


async def _from_file(bot: Bot, music: MusicService, owner: int, file_id: str, name: str,
                     title: str, mime: str, size: int | None) -> Track | PendingTrack:
    if size and size > MAX_TRACK_BYTES:
        raise MusicError(f"трек больше {MAX_TRACK_BYTES // 1024 // 1024} МБ — пришли mp3 "
                         "полегче или ссылку на YouTube")
    try:
        buf = await bot.download(file_id)
    except TelegramBadRequest as exc:
        if "too big" in str(exc).lower():
            raise MusicError("файл больше 20 МБ — Telegram его не отдаёт. Пришли mp3 полегче "
                             "или ссылку на YouTube") from exc
        raise
    if buf is None:
        raise MusicError("не смог скачать файл из Telegram")
    return await music.prepare_upload(owner, buf.read(), name, title=title, mime=mime)


async def _after_prepare(chat_id: int, owner: int, bot: Bot, state: FSMContext,
                         music: MusicService, job: Awaitable[Track | PendingTrack],
                         status: Message) -> None:
    try:
        res = await job
    except Exception as exc:
        await state.set_state(Flow.music)
        await bot.send_message(chat_id, _err(exc))
        return
    try:
        await status.delete()
    except TelegramBadRequest:
        pass
    if isinstance(res, Track):
        await bot.send_message(chat_id, "📚 Этот трек уже есть в библиотеке — беру его.")
        await _choose_and_preview(chat_id, bot, state, music, res.id)
        return
    track = await music.save_track(owner, res)
    m = res.map
    drops = ", ".join(view.mmss(d) for d in m.drops[:3]) or "явного нет"
    await bot.send_message(
        chat_id,
        f"🎵 <b>{escape(res.title)}</b> — сохранил в «Мои треки».\n"
        f"Длительность {view.mmss(m.duration)} · темп {m.bpm:.0f} BPM · дроп: {drops}")
    await _choose_and_preview(chat_id, bot, state, music, track.id)


# ---------- библиотека ----------

@router.callback_query(Flow.music, F.data == "mu:lib")
@router.callback_query(Flow.music_preview, F.data == "mu:lib")
async def cb_library(cb: CallbackQuery, state: FSMContext, music: MusicService) -> None:
    await cb.answer()
    if not isinstance(cb.message, Message):
        return
    tracks = await music.tracks(cb.from_user.id)
    if not tracks:
        await cb.message.answer("📚 Библиотека пока пустая — пришли трек файлом.")
        return
    await state.set_state(Flow.music)
    await cb.message.answer("📚 <b>Мои треки</b>", reply_markup=view.library_kb(tracks))


@router.callback_query(Flow.music, F.data.startswith("mu:t:"))
async def cb_pick_track(cb: CallbackQuery, bot: Bot, state: FSMContext,
                        music: MusicService) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        await _choose_and_preview(cb.message.chat.id, bot, state, music, _cb_id(cb))


def _cb_id(cb: CallbackQuery) -> int:
    """id трека из хвоста callback_data: «mu:t:5», «mu:r:5»…"""
    return int((cb.data or ":0").rsplit(":", 1)[1])


# ---------- переименование и удаление ----------

@router.callback_query(LIBRARY, F.data == "mu:manage")
async def cb_manage(cb: CallbackQuery, music: MusicService) -> None:
    await cb.answer()
    if not isinstance(cb.message, Message):
        return
    tracks = await music.tracks(cb.from_user.id)
    if not tracks:
        await cb.message.answer("📚 Библиотека пустая.")
        return
    await cb.message.answer("✏️ <b>Какой трек поправить?</b>",
                            reply_markup=view.manage_kb(tracks))


@router.callback_query(LIBRARY, F.data.startswith("mu:m:"))
async def cb_track_card(cb: CallbackQuery, music: MusicService) -> None:
    await cb.answer()
    if not isinstance(cb.message, Message):
        return
    try:
        t = await music.track(cb.from_user.id, _cb_id(cb))
    except Exception as exc:
        await cb.message.answer(_err(exc))
        return
    await cb.message.answer(view.track_card(t), reply_markup=view.track_kb(t.id))


@router.callback_query(LIBRARY, F.data.startswith("mu:r:"))
async def cb_rename(cb: CallbackQuery, state: FSMContext, music: MusicService) -> None:
    await cb.answer()
    if not isinstance(cb.message, Message):
        return
    try:
        t = await music.track(cb.from_user.id, _cb_id(cb))
    except Exception as exc:
        await cb.message.answer(_err(exc))
        return
    # Запоминаем, откуда пришли: после переименования вернём туда же.
    await state.update_data(rename_id=t.id, rename_back=await state.get_state())
    await state.set_state(Flow.music_rename)
    await cb.message.answer(view.rename_prompt(t.title), reply_markup=view.RENAME_CANCEL_KB)


@router.message(Flow.music_rename, F.text & NOT_COMMAND)
async def got_rename(message: Message, bot: Bot, state: FSMContext,
                     music: MusicService) -> None:
    owner = message.from_user.id if message.from_user else 0
    track_id = int((await state.get_data()).get("rename_id") or 0)
    try:
        t = await music.rename_track(owner, track_id, message.text or "")
    except Exception as exc:
        await message.answer(_err(exc))
        return
    await message.answer(f"✅ Теперь трек называется <b>{escape(t.title)}</b>.")
    await _rename_back(message.chat.id, bot, state, music, owner)


@router.message(Flow.music_rename, ~F.text)
async def rename_needs_text(message: Message) -> None:
    await message.answer("✏️ Напиши название текстом — или нажми «↩️ Не менять».")


@router.callback_query(Flow.music_rename, F.data == "mu:rc")
async def cb_rename_cancel(cb: CallbackQuery, bot: Bot, state: FSMContext,
                           music: MusicService) -> None:
    await cb.answer("Оставил как было")
    if isinstance(cb.message, Message):
        await cb.message.edit_reply_markup(reply_markup=None)
        await _rename_back(cb.message.chat.id, bot, state, music, cb.from_user.id)


async def _rename_back(chat_id: int, bot: Bot, state: FSMContext, music: MusicService,
                       owner: int) -> None:
    """Пришли из превью — кнопки превью прямо тут, чтобы не листать вверх.
    Из библиотеки — свежий список с новым названием."""
    data = await state.get_data()
    if data.get("rename_back") == Flow.music_preview.state and data.get("track_id"):
        await state.set_state(Flow.music_preview)
        await bot.send_message(chat_id, "🎵 Что с куском?",
                               reply_markup=view.preview_kb(int(data["track_id"])))
        return
    await state.set_state(Flow.music)
    tracks = await music.tracks(owner)
    if not tracks:
        await _menu(chat_id, bot, state, music, owner)
        return
    await bot.send_message(chat_id, "📚 <b>Мои треки</b>", reply_markup=view.library_kb(tracks))


@router.callback_query(LIBRARY, F.data.startswith("mu:d:"))
async def cb_delete(cb: CallbackQuery, music: MusicService) -> None:
    await cb.answer()
    if not isinstance(cb.message, Message):
        return
    try:
        t = await music.track(cb.from_user.id, _cb_id(cb))
    except Exception as exc:
        await cb.message.answer(_err(exc))
        return
    await cb.message.answer(
        f"🗑 Удалить <b>{escape(t.title)}</b> из «Моих треков»?\n"
        "Файл тоже сотру, ролики с этим треком останутся без музыки.",
        reply_markup=view.delete_kb(t.id))


@router.callback_query(LIBRARY, F.data == "mu:keep")
async def cb_keep(cb: CallbackQuery) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        await cb.message.edit_text("👌 Трек оставил.")


@router.callback_query(LIBRARY, F.data.startswith("mu:dy:"))
async def cb_delete_yes(cb: CallbackQuery, bot: Bot, state: FSMContext,
                        music: MusicService) -> None:
    await cb.answer()
    if not isinstance(cb.message, Message):
        return
    track_id, owner, chat_id = _cb_id(cb), cb.from_user.id, cb.message.chat.id
    try:
        title = await music.delete_track(owner, track_id)
    except Exception as exc:
        await cb.message.answer(_err(exc))
        return
    await cb.message.edit_text(f"🗑 Удалил <b>{escape(title)}</b>.")
    if int((await state.get_data()).get("track_id") or 0) == track_id:
        # Это был трек текущего ролика — ролик остался без музыки, выбираем заново.
        await state.update_data(track_id=None)
        await _menu(chat_id, bot, state, music, owner)
        return
    if await state.get_state() == Flow.music.state:
        tracks = await music.tracks(owner)
        if tracks:
            await bot.send_message(chat_id, "📚 <b>Мои треки</b>",
                                   reply_markup=view.library_kb(tracks))
        else:
            await _menu(chat_id, bot, state, music, owner)
    # В превью ничего не шлём: кнопки куска выше по-прежнему работают.


# ---------- кусок и превью ----------

async def _choose_and_preview(chat_id: int, bot: Bot, state: FSMContext, music: MusicService,
                              track_id: int) -> None:
    try:
        project_id, _ = await _ids(state)
        choice = await music.choose(project_id, track_id)
    except Exception as exc:
        await bot.send_message(chat_id, _err(exc))
        return
    await state.update_data(track_id=track_id)
    await _send_preview(chat_id, bot, state, music, choice, note="подобрал сам")


async def _send_preview(chat_id: int, bot: Bot, state: FSMContext, music: MusicService,
                        choice: Choice, note: str = "") -> None:
    await state.set_state(Flow.music_preview)
    mp3 = await music.preview(choice)
    project_id, _ = await _ids(state)
    await bot.send_audio(
        chat_id, BufferedInputFile(mp3, filename="preview.mp3"),
        title=f"{choice.track.title} · кусок для ролика", performer="AdReel",
        caption=view.preview_caption(choice, note, await music.key_at(project_id)),
        reply_markup=view.preview_kb(choice.track.id))


@router.callback_query(Flow.music_preview, F.data == "mu:next")
async def cb_next(cb: CallbackQuery, bot: Bot, state: FSMContext, music: MusicService) -> None:
    await cb.answer()
    if not isinstance(cb.message, Message):
        return
    try:
        project_id, _ = await _ids(state)
        choice = await music.next_window(project_id)
    except Exception as exc:
        await cb.message.answer(_err(exc))
        return
    await _send_preview(cb.message.chat.id, bot, state, music, choice,
                        note=f"вариант {choice.index + 1}")


@router.callback_query(Flow.music_preview, F.data == "mu:seg")
async def cb_segment(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.set_state(Flow.music_segment)
    if isinstance(cb.message, Message):
        await cb.message.answer(view.SEGMENT_HELP)


@router.message(Flow.music_segment, F.voice | F.audio | (F.text & NOT_COMMAND))
async def got_segment(message: Message, bot: Bot, state: FSMContext, music: MusicService,
                      svc: ProductService) -> None:
    try:
        project_id, pid = await _ids(state)
        spec = message.text or ""
        voice = message.voice or message.audio
        if voice is not None:
            buf = await bot.download(voice.file_id)
            if buf is None:
                raise MusicError("не смог скачать голосовое")
            spec = await svc.transcribe(pid, buf.read(), "voice.ogg", float(voice.duration or 0))
            await message.answer(f"🎙 <i>{escape(spec)}</i>")
        track_id = (await state.get_data()).get("track_id")
        if not track_id:
            raise MusicError("сначала выбери трек")
        choice, note = await music.choose_custom(project_id, int(track_id), spec)
    except Exception as exc:
        await message.answer(_err(exc))
        return
    await _send_preview(message.chat.id, bot, state, music, choice, note=note or "твой кусок")


@router.callback_query(Flow.music_preview, F.data == "mu:other")
async def cb_other(cb: CallbackQuery, bot: Bot, state: FSMContext, music: MusicService) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        await _menu(cb.message.chat.id, bot, state, music, cb.from_user.id)


@router.callback_query(Flow.music_preview, F.data == "mu:ok")
async def cb_take(cb: CallbackQuery, bot: Bot, state: FSMContext,
                  music: MusicService) -> None:
    await cb.answer()
    if not isinstance(cb.message, Message):
        return
    await cb.message.edit_reply_markup(reply_markup=None)
    await _to_final(cb.message.chat.id, bot, state, music)


@router.callback_query(Flow.music, F.data == "mu:none")
async def cb_none(cb: CallbackQuery, bot: Bot, state: FSMContext,
                  music: MusicService) -> None:
    await cb.answer()
    if not isinstance(cb.message, Message):
        return
    project_id, _ = await _ids(state)
    await music.no_music(project_id)
    await cb.message.answer("🔇 Без музыки — только голос.")
    await _to_final(cb.message.chat.id, bot, state, music)


async def _to_final(chat_id: int, bot: Bot, state: FSMContext, music: MusicService) -> None:
    """Дальше — итоговый экран перед генерацией.
    Импорт внутри функции: creative_flow импортирует нас, иначе кольцо."""
    from .creative_flow import open_final

    await open_final(chat_id, bot, state, music)


@router.message(Flow.music_busy)
async def busy(message: Message) -> None:
    await message.answer("⏳ Ещё слушаю трек — секунду.")


@router.message(Flow.music_preview, NOT_COMMAND)
async def use_buttons(message: Message) -> None:
    await message.answer("👆 Выбери кнопкой под сообщением выше.")
