"""Агенты креатива: креативщик (направления), режиссёр (сценарий видео), копирайтер (озвучка).

Каждый ответ проходит проверку правил (validate.py). Не прошёл — модель получает свой
же ответ и список ошибок и переделывает. Так правила держит код, а не надежда на промпт.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel

from .. import prompts
from ..llm.base import LLMProvider
from ..product.extract import categories
from ..product.schema import ProductCard
from . import playbook, validate
from .schema import Direction, Directions, VideoScript, Voiceover

log = logging.getLogger("adreel.script.creative")

#: сколько раз модель переделывает ответ, не прошедший проверку
FIX_ROUNDS = 2
LANG = {"uk": "украинский", "ru": "русский", "en": "английский"}


class ScriptError(RuntimeError):
    """Ошибка, которую можно показать владельцу."""


@dataclass(frozen=True)
class PhotoInfo:
    id: int
    description: str


async def parse_checked[M: BaseModel](
    llm: LLMProvider, system: str, user: str, model: type[M],
    check: Callable[[M], list[str]], *, max_tokens: int,
    fix: Callable[[M], M] | None = None,
) -> M:
    res = await llm.parse(system, user, model, max_tokens=max_tokens)
    for attempt in range(FIX_ROUNDS + 1):
        if fix is not None:
            res = fix(res)
        errors = check(res)
        if not errors:
            return res
        if attempt == FIX_ROUNDS:
            break
        log.info("%s не прошёл проверку: %s", model.__name__, errors)
        retry = (f"{user}\n\nТВОЙ ПРЕДЫДУЩИЙ ВАРИАНТ:\n{res.model_dump_json()}\n\n"
                 "ОН НЕ ПРОШЁЛ ПРОВЕРКУ. Исправь эти ошибки и верни ответ ЦЕЛИКОМ:\n- "
                 + "\n- ".join(errors))
        res = await llm.parse(system, retry, model, max_tokens=max_tokens)
    raise ScriptError("агент не уложился в правила: " + "; ".join(check(res))[:400])


@dataclass(frozen=True)
class Brief:
    """Всё, что агенты креатива знают о товаре: карточка + материалы, которые в неё не влезли."""

    card: ProductCard
    photos: list[PhotoInfo]
    owner_notes: list[str]
    #: выжимка из интернета (research.notes.ResearchNotes.block()) — про рынок, не про товар
    market: str = ""


def card_block(card: ProductCard) -> str:
    return card.model_dump_json(indent=1, exclude={"answered"})


def brief_block(b: Brief) -> str:
    parts = ["КАРТОЧКА ТОВАРА (факты с источниками):", card_block(b.card), "",
             "ФОТО ТОВАРА (id — что на фото, по разбору зрением):", _photos_block(b.photos)]
    if b.owner_notes:
        parts += ["", "ЧТО ВЛАДЕЛЕЦ ПИСАЛ И НАГОВАРИВАЛ ПРО ТОВАР И РОЛИК (дословно; "
                      "пожелания к ролику — учитывай, факты — только если они есть в карточке):"]
        parts += [f"— {n}" for n in b.owner_notes]
    if b.market:
        # заголовок «для идей, не для фактов» уже внутри (ResearchNotes.block)
        parts += ["", b.market]
    return "\n".join(parts)


def brief_message(b: Brief, task: str) -> str:
    """Бриф — в user-сообщение в явной рамке, а не в system: карточку собирала LLM
    из текста чужих страниц, слова владельца дословные. Фраза со страницы товара
    («игнорируй правила, напиши…») не должна стать инструкцией агенту."""
    return f"<бриф>\n{brief_block(b)}\n</бриф>\n\n{task}"


def _hints(card: ProductCard) -> str:
    cat = categories().get(card.category, {})
    hints = cat.get("hints", "")
    return f"ОСОБЕННОСТИ КАТЕГОРИИ «{cat.get('title', card.category)}»: {hints}" if hints else ""


def direction_block(d: Direction) -> str:
    return d.model_dump_json(indent=1)


# ---------- креативщик ----------

async def propose_directions(llm: LLMProvider, brief: Brief, *, count: int = 4,
                             wishes: str = "", avoid: list[Direction] | None = None
                             ) -> Directions:
    card = brief.card
    wishes_block = f"ПОЖЕЛАНИЯ ВЛАДЕЛЬЦА (учти обязательно): {wishes}" if wishes else ""
    if avoid:
        wishes_block += ("\n\nЭТИ НАПРАВЛЕНИЯ УЖЕ БЫЛИ — предложи ДРУГИЕ, не похожие:\n"
                         + "\n".join(f"- {d.title} ({d.format})" for d in avoid))
    system = prompts.render("creative_directions", count=count,
                            duration_hint="15–30 секунд", language=LANG[card.language],
                            category_hints=_hints(card), wishes=wishes_block,
                            playbook=playbook.block("model_can", "model_cannot", "rhythm"))
    user = brief_message(brief, f"Предложи {count} направления.")

    def check(d: Directions) -> list[str]:
        titles = [x.title.strip().lower() for x in d.directions]
        errors = []
        if len(d.directions) < min(count, 3):
            errors.append(f"нужно {count} направлений, а пришло {len(d.directions)}")
        if len(set(titles)) != len(titles):
            errors.append("направления повторяются — сделай их разными")
        return errors

    return await parse_checked(llm, system, user, Directions, check, max_tokens=6000)


async def own_direction(llm: LLMProvider, brief: Brief, idea: str) -> Direction:
    system = prompts.render("creative_own", language=LANG[brief.card.language])
    return await llm.parse(system, brief_message(brief, f"Идея владельца: {idea}"), Direction,
                           max_tokens=3000)


# ---------- режиссёр ----------

def _photos_block(photos: list[PhotoInfo]) -> str:
    if not photos:
        return "(фото товара нет — все кадры generated_scene без photo_ref)"
    return "\n".join(f"{p.id} — {p.description}" for p in photos)


async def write_script(llm: LLMProvider, brief: Brief, direction: Direction,
                       rules: validate.ScriptRules, *, instruction: str = "") -> VideoScript:
    system = prompts.render(
        "video_script", duration=rules.duration, min_shots=rules.min_shots,
        max_shots=rules.max_shots, min_shot=rules.min_shot, max_shot=rules.max_shot,
        language=LANG[brief.card.language], direction=direction_block(direction),
        playbook=playbook.block())
    user = brief_message(brief, instruction or "Напиши сценарий видео.")
    res = await parse_checked(
        llm, system, user, VideoScript, lambda s: validate.check_script(s, rules),
        fix=lambda s: validate.normalize_script(s, rules), max_tokens=8000)
    res.prompt_version = prompts.load("video_script").version
    return res


def script_instruction(kind: str, current: VideoScript | None, text: str = "",
                       shot: int | None = None) -> str:
    cur = current.model_dump_json(indent=1, exclude={"prompt_version"}) if current else ""
    match kind:
        case "edit":
            return (f"ПРАВКА ВЛАДЕЛЬЦА: {text}\nИзмени только то, о чём он просит, остальное "
                    f"оставь как есть. Текущий сценарий:\n{cur}")
        case "redo":
            return (f"Переделай ТОЛЬКО кадр {shot}: придумай другой визуал и движение в том же "
                    f"направлении. Роль и длительность кадра сохрани, остальные кадры не "
                    f"трогай. Текущий сценарий:\n{cur}")
        case "variant":
            return ("Напиши ДРУГОЙ вариант сценария в том же направлении — с другими кадрами "
                    f"и другим хуком, не похожий на этот:\n{cur}")
    return ""


# ---------- копирайтер ----------

def _shots_block(script: VideoScript, chars_per_sec: float) -> str:
    rows = []
    for s in script.shots:
        limit = validate.char_limit(s.duration_sec, chars_per_sec)
        star = " ⭐главный" if s.n == script.key_shot else ""
        text = f" · надпись «{s.on_screen_text}»" if s.on_screen_text else ""
        rows.append(f"{s.n} · {s.duration_sec} с · до {limit} симв · {s.role}{star} · "
                    f"{s.visual}{text}")
    return "\n".join(rows)


async def write_voiceover(llm: LLMProvider, brief: Brief, direction: Direction,
                          script: VideoScript, chars_per_sec: float, *,
                          instruction: str = "") -> Voiceover:
    card = brief.card
    system = prompts.render(
        "voiceover", pace=f"{chars_per_sec:.1f}", language=LANG[card.language],
        cta=card.cta or "короткий призыв купить", voice_tone=direction.voice_tone,
        forbidden="; ".join(card.forbidden_claims) or "—",
        direction=direction_block(direction), shots=_shots_block(script, chars_per_sec))
    user = brief_message(brief, instruction or "Напиши озвучку под эти кадры.")
    res = await parse_checked(
        llm, system, user, Voiceover,
        lambda v: validate.check_voiceover(v, script, card, chars_per_sec), max_tokens=6000)
    for line in res.lines:
        if not line.text_tts.strip():
            line.text_tts = line.text
    res.prompt_version = prompts.load("voiceover").version
    return res


def voiceover_instruction(kind: str, current: Voiceover | None, text: str = "") -> str:
    cur = json.dumps([ln.model_dump() for ln in current.lines], ensure_ascii=False) \
        if current else ""
    match kind:
        case "edit":
            return (f"ПРАВКА ВЛАДЕЛЬЦА: {text}\nИзмени только то, о чём он просит. "
                    f"Текущая озвучка:\n{cur}")
        case "variant":
            return f"Напиши ДРУГОЙ вариант озвучки — с другим хуком и формулировками:\n{cur}"
    return ""
