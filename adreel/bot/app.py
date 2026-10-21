"""Сборка и запуск бота: сессия (облачный или локальный Bot API), доступ только владельцу."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import sys
from collections.abc import Awaitable, Callable
from html import escape
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, CallbackQuery, ErrorEvent, Message, TelegramObject

from .. import health
from ..config import ROOT, settings
from ..db.session import make_engine, make_sessions, upgrade
from ..gen import make_image_gen, make_video_gen
from ..gen.models import image_model, video_model
from ..gen.pipeline import Deps as GenDeps
from ..gen.pipeline import GenerationService
from ..ingest.media import MediaStore
from ..ingest.stt import make_stt
from ..llm import creative_llm, text_llm, vision_llm
from ..music.service import MusicService
from ..product.service import Deps, ProductService
from ..script.service import ScriptService
from ..tts.elevenlabs import make_tts
from . import creative_flow, music_flow, product_flow, view
from .states import Flow
from .storage import JsonFileStorage

log = logging.getLogger("adreel.bot")

router = Router(name="base")

def is_owner(user_id: int | None) -> bool:
    return bool(settings.owner_telegram_id) and user_id == settings.owner_telegram_id


class OwnerOnly(BaseMiddleware):
    """Бот личный: генерация тратит реальные деньги, а ботов находят сканеры."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        uid = user.id if user else None
        if is_owner(uid):
            return await handler(event, data)
        if isinstance(event, Message) and (event.text or "").split("@")[0].strip() == "/id":
            # Своё id не секрет, а без него OWNER_TELEGRAM_ID в .env не заполнить.
            await event.answer(f"Твой Telegram ID: <code>{uid}</code>\n"
                               "Впиши его в .env как OWNER_TELEGRAM_ID и перезапусти бота.")
            return None
        log.warning("отказано пользователю %s", uid)
        if isinstance(event, Message):
            await event.answer("⛔️ Это личный бот. Доступа нет.")
        elif isinstance(event, CallbackQuery):
            await event.answer("Доступа нет", show_alert=True)
        return None


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message) -> None:
    await message.answer(view.HELP)


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    uid = message.from_user.id if message.from_user else 0
    await message.answer(f"Твой Telegram ID: <code>{uid}</code>")


async def on_error(event: ErrorEvent) -> bool:
    """Последняя сеть: без неё человек видит молчание и думает, что бот умер."""
    log.exception("непойманная ошибка: %s", event.exception)
    upd = event.update
    # режем ДО экранирования: обрезанная «&amp;» ломает HTML, и Telegram отклонит само
    # сообщение об ошибке
    text = f"⚠️ <b>Ошибка внутри бота.</b>\n<code>{escape(str(event.exception)[:300])}</code>"
    try:
        if upd.callback_query and upd.callback_query.message:
            await upd.callback_query.answer("Ошибка — смотри сообщение ниже", show_alert=True)
            await upd.callback_query.message.answer(text)
        elif upd.message:
            await upd.message.answer(text)
    except Exception:
        log.exception("не смог сообщить об ошибке")
    return True


def make_bot() -> Bot:
    session = None
    if settings.telegram_api_base:
        api = TelegramAPIServer.from_base(settings.telegram_api_base.rstrip("/"), is_local=True)
        session = AiohttpSession(api=api)
        log.info("Bot API: локальный сервер %s", settings.telegram_api_base)
    return Bot(token=settings.telegram_bot_token, session=session,
               default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def setup_logging() -> None:
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_h = logging.handlers.RotatingFileHandler(
        logs / "bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    file_h.setFormatter(fmt)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[console, file_h])


async def main() -> int:
    setup_logging()
    for c in health.preflight():
        if not c.ok:
            log.warning("не готово: %s %s", c.name, c.detail)
    if not settings.telegram_bot_token:
        log.error("старт невозможен: нет TELEGRAM_BOT_TOKEN (.env)")
        return 2

    settings.workdir.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(upgrade, settings.db_url)
    engine = make_engine(settings.db_url)
    svc = ProductService(Deps(
        sessions=make_sessions(engine),
        media=MediaStore(settings.workdir),
        text_llm=text_llm,
        vision_llm=vision_llm,
        stt=make_stt,
    ))

    music = MusicService(make_sessions(engine), MediaStore(settings.workdir))
    scripts = ScriptService(make_sessions(engine), svc, creative_llm)

    gen = GenerationService(GenDeps(
        sessions=make_sessions(engine), media=MediaStore(settings.workdir), products=svc,
        scripts=scripts, imagegen=make_image_gen, videogen=make_video_gen,
        image_model=image_model, video_model=video_model, music=music,
        tts=make_tts, fonts_dir=settings.fonts_dir))

    storage = JsonFileStorage(settings.workdir / "dialogs.json")
    revived = storage.revive({Flow.busy.state or "": Flow.collecting.state or "",
                              Flow.music_busy.state or "": Flow.music.state or "",
                              Flow.creative_busy.state or "": Flow.ready.state or "",
                              Flow.generating.state or "": Flow.done.state or ""})

    bot = make_bot()
    dp = Dispatcher(storage=storage)
    dp["svc"] = svc
    dp["music"] = music
    dp["scripts"] = scripts
    dp["gen"] = gen
    creative_flow.set_scripts(scripts)
    guard = OwnerOnly()
    dp.message.outer_middleware(guard)
    dp.callback_query.outer_middleware(guard)
    dp.include_router(router)
    dp.include_router(creative_flow.router)
    dp.include_router(music_flow.router)
    dp.include_router(product_flow.router)   # последним: там обработчик «всё остальное»
    dp.errors.register(on_error)

    await bot.set_my_commands([
        BotCommand(command="new", description="Новый ролик: выбрать или добавить товар"),
        BotCommand(command="help", description="Что это за бот и как им пользоваться"),
    ])
    for chat_id, fallback in revived:
        if fallback == Flow.music.state:
            await bot.send_message(chat_id, "⚠️ Бот перезапускался, пока я слушал трек. "
                                            "Пришли его ещё раз.")
        elif fallback == Flow.done.state:
            await bot.send_message(chat_id, "⚠️ Бот перезапускался во время генерации. "
                                            "Готовые сцены сохранены — нажми «Доделать»: "
                                            "продолжу с места сбоя, оплаченное подберётся "
                                            "без новой оплаты.",
                                   reply_markup=view.failed_kb())
        elif fallback == Flow.ready.state:
            await bot.send_message(chat_id, "⚠️ Бот перезапускался, пока агент думал. "
                                            "Нажми /new, выбери товар — продолжим.")
        else:
            await bot.send_message(chat_id, "⚠️ Бот перезапускался, пока шёл разбор. "
                                            "Материалы на месте — нажми «Готово» ещё раз.",
                                   reply_markup=view.collect_kb())
    me = await bot.get_me()
    log.info("бот @%s запущен; владелец: %s", me.username, settings.owner_telegram_id or "НЕ ЗАДАН")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        await engine.dispose()
    return 0


def run() -> int:
    try:
        return asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        return 0
