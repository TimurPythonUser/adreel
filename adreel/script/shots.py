"""Оператор: промпты под видеомодель и план непрерывности между сценами.

Промпт H3 — не абзац, а документ из трёх полей (официальный гайд MiniMax-H3):

    <строка выравнивания картинки>
    integrated_multimodal_description:
    [Shot 1] ...
    overall_soundscape: ...
    non_diegetic_music: N/A

Собирает его КОД (build_prompt), агент даёт только смысловые куски: что сохранить,
что происходит, чего избегать. Так формат нельзя забыть или переврать, а контракт
сохранения товара всегда стоит первым — там он весит больше всего.

Группы. Одна генерация может держать несколько сцен: блоки [Shot 1], [Shot 2] At
00:02.500 ... Модель режет внутри генерации сама, и эти склейки самые честные — свет,
цвет и мир одни на всю генерацию. Плюс у модели минимум 5 секунд на вызов, поэтому две
короткие сцены дешевле снять одной генерацией, чем двумя. Готовый клип код сам режет
на куски по сценам, дальше монтаж работает как обычно.

Непрерывность между сценами:
  • frame — следующая сцена стартует с последнего кадра прошлого клипа. Дёшево, держит
    картинку, но камера остаётся стоять там же, где стояла;
  • reference — в модель уходит ХВОСТ прошлого клипа: она видит движение и продолжает
    его, можно уехать на новый ракурс. Дороже, поэтому только на ключевых стыках.
Остальное прячет монтаж: режем на движении (cut_note) и парными фразами переходов.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from .. import prompts
from ..gen.models import VideoModel
from ..llm.base import LLMProvider
from . import playbook
from .creative import Brief, brief_message, direction_block, parse_checked
from .schema import Direction, VideoScript

StartFrom = Literal["product_photo", "generated_scene", "prev_last_frame"]
ChainMode = Literal["frame", "reference"]
EndWith = Literal["none", "next_start", "product_photo"]
Transition = Literal["cut", "match_cut", "whip_pan", "object_wipe", "speed_ramp", "morph"]

#: переходы, которые живут в ХВОСТЕ клипа: монтаж должен взять конец клипа, а не начало.
#: Урок 23.10.2026: клип генерится на 5 с, сцена длится 2.5 с, монтаж брал начало —
#: смаз и шторка в конце клипа просто отрезались
TAIL_TRANSITIONS = ("whip_pan", "object_wipe", "speed_ramp", "morph")
#: переходы, которые следующая сцена подхватывает в своём НАЧАЛЕ (выход из смаза, шторки)
HEAD_TRANSITIONS = ("whip_pan", "object_wipe")
#: клип длиннее сцены настолько — уже надо говорить модели, когда случается действие
PACING_GAP = 0.75

#: сплошная цепочка превращает ролик в один долгий план и копит искажения.
#: Практика та же: переякориваться свежим кадром каждые 3–4 сцены.
MAX_CHAIN_RUN = 3
#: дорогих бесшовных продолжений — не больше двух на ролик: это приём для главного
#: поворота, а не для каждого стыка (иначе зритель устаёт, а счёт растёт)
MAX_REFERENCE_CHAINS = 2
#: доля латиницы, ниже которой считаем, что промпт написан не по-английски
LATIN_SHARE = 0.55

#: слова про монтаж: сцена — это ОДИН непрерывный кадр
BANNED_IN_PROMPT = ("cut to", "next scene", "transition to", "split screen", "montage")
#: модель рисует надписи нечитаемо — текст кладём оверлеем на монтаже
BANNED_GRAPHICS = ("on-screen text", "text overlay", "caption", "subtitle", "logo",
                   "watermark", "title card", "lower third")
#: говорящий человек не совпадёт с нашей озвучкой по губам
SPEECH_WORDS = ("says", "saying", "speaks", "speaking", "talks", "talking", "dialogue",
                "sings", "singing", "lip sync", "lip-sync")
CLOSED_LIPS = "lips remain completely closed"
#: официальный словарь движения камеры H3 (с окончаниями: pushes in, arcs, tracking)
CAMERA_RE = re.compile(
    r"\b(?:push|pull|zoom|pan|truck|tilt|pedestal|arc|track|shake|roll|dolly|orbit)(?:e?s|ing)?\b"
    r"|\b(?:static|pov|handheld|locked-off)\b", re.I)
#: музыку модели не заказываем никогда: музыка своя, и у каждой генерации она была бы своя
MUSIC_WORDS = ("music", "soundtrack", "score", "melody", "song")

#: шторка передним планом: в хвосте сцены предмет закрывает объектив, следующая
#: открывается из-за него. Работает, только если попросили с обеих сторон
WIPE_OUT = ("in front of the lens", "filling the frame", "fills the frame", "covers the lens",
            "blocks the frame", "foreground")
WIPE_IN = ("reveal", "out of frame", "clears the frame", "slides away", "moves away from the lens")

#: негативы, которые уходят в каждый промпт (негативная формулировка работает у H3)
DEFAULT_AVOID = ("no soft dissolves or morphs", "no on-screen text or logos",
                 "no extra limbs or duplicated products", "no warping of the product")


class ShotPlan(BaseModel):
    n: int
    preserve: str = Field(description="контракт сохранения, английский: что НЕ меняется "
                                      "(силуэт, пропорции, материал, цвет, упаковка)")
    description: str = Field(description="тело [Shot 1], английский: стиль, что в кадре, "
                                         "одно действие, движение камеры (тип+амплитуда+"
                                         "скорость), свет")
    avoid: str = Field(default="", description="что модель любит испортить именно здесь, "
                                               "английский, через запятую")
    soundscape: str = Field(default="", description="звуки В КАДРЕ одной фразой, английский; "
                                                    "пусто — N/A. Музыку не заказывать")
    start_from: StartFrom
    chain: ChainMode = Field(default="frame",
                             description="как продолжаем прошлую сцену: frame — со стоп-кадра, "
                                         "reference — с хвоста клипа (дороже, бесшовно)")
    group_with_prev: bool = Field(
        default=False,
        description="снимать эту сцену ТОЙ ЖЕ генерацией, что и предыдущую: модель сама "
                    "режет внутри клипа. Дешевле и стык честнее, но мир и свет общие")
    end_with: EndWith = "none"
    transition: Transition = "cut"
    cut_note: str = Field(default="", description="где резать эту сцену, по-русски")


class ShotPlans(BaseModel):
    style_anchor: str = Field(description="общий хвост всех промптов: оптика, свет, цвет")
    shots: list[ShotPlan]
    prompt_version: str = ""

    def plan_for(self, n: int) -> ShotPlan:
        return next(s for s in self.shots if s.n == n)

    def prompt_for(self, n: int, *, duration: int = 0, end_image: bool = False,
                   reference: bool = False) -> str:
        """Готовый промпт сцены в официальном формате модели."""
        return build_prompt([self.plan_for(n)], self.style_anchor, duration=duration,
                            end_image=end_image, reference=reference)

    def groups(self) -> list[list[ShotPlan]]:
        """Сцены, разбитые на генерации: подряд идущие group_with_prev едут вместе."""
        out: list[list[ShotPlan]] = []
        for plan in self.shots:
            if plan.group_with_prev and out:
                out[-1].append(plan)
            else:
                out.append([plan])
        return out

    def group_of(self, n: int) -> list[ShotPlan]:
        return next(g for g in self.groups() if any(p.n == n for p in g))

    def group_prompt(self, group: list[ShotPlan], offsets: list[float], *, duration: int = 0,
                     end_image: bool = False, reference: bool = False, pacing: str = "",
                     product_ref: bool = True) -> str:
        return build_prompt(group, self.style_anchor, offsets=offsets, duration=duration,
                            end_image=end_image, reference=reference, pacing=pacing,
                            product_ref=product_ref)

    def _neighbours(self, n: int) -> tuple[ShotPlan | None, ShotPlan, ShotPlan | None]:
        i = next(k for k, s in enumerate(self.shots) if s.n == n)
        return (self.shots[i - 1] if i else None, self.shots[i],
                self.shots[i + 1] if i + 1 < len(self.shots) else None)

    def needs_tail(self, n: int) -> bool:
        """Монтажу нужен КОНЕЦ клипа этой сцены, а не начало: там переход, конечный кадр
        или с последнего кадра стартует следующая сцена (иначе на стыке скачок)."""
        _, plan, nxt = self._neighbours(n)
        if plan.transition in TAIL_TRANSITIONS or plan.end_with != "none":
            return True
        return (nxt is not None and nxt.start_from == "prev_last_frame"
                and not nxt.group_with_prev)

    def needs_head(self, n: int) -> bool:
        """Сцена начинается выходом из смаза или шторки предыдущей — нужно её начало."""
        prev, plan, _ = self._neighbours(n)
        return (prev is not None and prev.transition in HEAD_TRANSITIONS
                and not plan.group_with_prev)

    @property
    def chained(self) -> list[int]:
        return [s.n for s in self.shots if s.start_from == "prev_last_frame"]

    @property
    def references(self) -> list[int]:
        return [s.n for s in self.shots if s.start_from == "prev_last_frame"
                and s.chain == "reference" and not s.group_with_prev]


def _timecode(seconds: float) -> str:
    """00:03.500 — как просит официальный гайд H3."""
    return f"{int(seconds // 60):02d}:{seconds % 60:06.3f}"


def pacing_line(scene_sec: float, clip_sec: float, tail: bool) -> str:
    """Когда в клипе случается действие, если клип длиннее сцены.

    Монтаж возьмёт из клипа только scene_sec: начало или конец. Если действие модель
    поставит в середину пятисекундного клипа, а сцена 2.5 с — в ролик попадёт пустой
    кусок. Фразу пишет код: длины знает только он.
    """
    if clip_sec - scene_sec < PACING_GAP:
        return ""
    if tail:
        return (f"Pacing: the shot opens calmly; the main action and the ending happen in the "
                f"final {scene_sec:.1f} seconds.")
    return (f"Pacing: the main action starts immediately and completes within the first "
            f"{scene_sec:.1f} seconds, then the shot settles.")


def _shot_block(plan: ShotPlan, style_anchor: str, n: int, at: float | None,
                pacing: str = "") -> str:
    """Один блок [Shot N]. У первой сцены таймкода нет, у остальных он обязателен."""
    head = f"[Shot {n}]" if at is None else f"[Shot {n}] At {_timecode(at)}, the camera cuts to:"
    body = " ".join(x for x in (plan.preserve.strip(), plan.description.strip(), pacing,
                                style_anchor.strip()) if x)
    avoid = ", ".join(dict.fromkeys([*[a.strip() for a in plan.avoid.split(",") if a.strip()],
                                     *DEFAULT_AVOID]))
    return f"{head} {body} Avoid: {avoid}."


def build_prompt(plans: list[ShotPlan], style_anchor: str, *, offsets: list[float] | None = None,
                 duration: int = 0, end_image: bool = False, reference: bool = False,
                 pacing: str = "", product_ref: bool = True) -> str:
    """Собрать промпт в формате H3: строка выравнивания + три поля.

    Строку выравнивания пишем сами: только код знает, что реально ушло в запрос —
    стартовый кадр, конечный кадр или хвост предыдущего клипа. Сцен в генерации может
    быть несколько: тогда у каждой свой блок [Shot N] с таймкодом склейки.
    pacing — фраза о том, когда случается действие (только для одной сцены в клипе).
    """
    head: list[str] = []
    if reference:
        # дословная формула продолжения: модель должна понять, что первый кадр новой
        # генерации — последний кадр присланного куска
        head.append("<Video 1> is the tail of the previous shot of the same commercial.")
        if product_ref:
            head.append("<Image 1> is the product reference; keep it identical.")
        head.append("Target video is a seamless continuation of <Video 1>. "
                    "First frame of [Shot 1] is the last frame of <Video 1>.")
    else:
        head.append("For the target video, at 0.00 seconds into the target video, "
                    "<Picture 1> (from [Shot 1]) is fully referenced.")
        if end_image and duration:
            head.append(f"<Picture 2> aligns with the video end at {duration}.00 seconds "
                        "and belongs to [Shot 1].")

    starts = offsets or [0.0] * len(plans)
    blocks = [_shot_block(p, style_anchor, i + 1, None if i == 0 else starts[i],
                          pacing if len(plans) == 1 else "")
              for i, p in enumerate(plans)]
    sounds = [p.soundscape.strip() for p in plans if p.soundscape.strip()]
    sound = " ".join(dict.fromkeys(sounds)) or "N/A"
    return ("\n".join(head) + "\n\nintegrated_multimodal_description:\n"
            + "\n".join(blocks)
            + f"\n\noverall_soundscape: {sound}\n"
            "non_diegetic_music: N/A")


def _latin_share(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(bool(re.match(r"[a-zA-Z]", c)) for c in letters) / len(letters)


def _pair_errors(plan: ShotPlan, nxt: ShotPlan | None) -> list[str]:
    """Переход живёт в ДВУХ сценах: хвост этой и начало следующей.

    Смаз, в котором прячется склейка, генерирует сама модель — но только если её
    попросили и в конце, и в начале. Бесплатный приём, поэтому требуем строго.
    """
    errors: list[str] = []
    cur, after = plan.description.lower(), (nxt.description.lower() if nxt else "")
    match plan.transition:
        case "whip_pan":
            if "whip" not in cur:
                errors.append(f"сцена {plan.n}: whip_pan — в конце описания нужен быстрый "
                              "смаз камеры (whips ... with motion blur)")
            if nxt and "whip" not in after and "motion blur" not in after:
                errors.append(f"сцена {nxt.n}: whip_pan — начало должно выходить из смаза "
                              "(camera decelerates from a fast pan, motion blur settling)")
        case "object_wipe":
            if not any(w in cur for w in WIPE_OUT):
                errors.append(f"сцена {plan.n}: object_wipe — в конце описания предмет или "
                              "человек проходит вплотную к объективу и закрывает кадр "
                              "(passes right in front of the lens, filling the frame)")
            if nxt and not any(w in after for w in WIPE_IN):
                errors.append(f"сцена {nxt.n}: object_wipe — начало должно открываться из-за "
                              "уходящего переднего плана (a foreground object slides out of "
                              "frame, revealing ...)")
        case "match_cut":
            if not plan.cut_note.strip():
                errors.append(f"сцена {plan.n}: match_cut — напиши в cut_note, какая форма "
                              "или движение совпадает со следующей сценой")
        case "speed_ramp":
            if not any(w in cur for w in ("accelerat", "speeds up", "faster")):
                errors.append(f"сцена {plan.n}: speed_ramp — опиши ускорение к концу сцены")
    return errors


def _group_errors(p: ShotPlans, script: VideoScript, model: VideoModel) -> list[str]:
    """Что можно и чего нельзя, когда несколько сцен снимаются одной генерацией.

    У сцены внутри группы нет своего стартового кадра — модель сама режет внутри клипа.
    Поэтому её start_from, chain и end_with не к чему применить, и мы требуем,
    чтобы агент их не выдумывал.
    """
    errors: list[str] = []
    seconds = {sh.n: sh.duration_sec for sh in script.shots}
    for group in p.groups():
        if len(group) == 1:
            continue
        total = sum(seconds.get(x.n, 0.0) for x in group)
        numbers = [x.n for x in group]
        if total > model.max_sec:
            errors.append(f"сцены {numbers} одной генерацией — это {total:g} с, а модель "
                          f"снимает максимум {model.max_sec} с. Разбей группу")
        for x in group[1:]:
            if x.start_from != "prev_last_frame":
                errors.append(f"сцена {x.n}: внутри группы нет своего стартового кадра — "
                              'start_from должен быть "prev_last_frame"')
            if x.chain != "frame":
                errors.append(f"сцена {x.n}: внутри группы chain всегда \"frame\" — "
                              "модель режет сама, хвост клипа ей не нужен")
        for x in group[:-1]:
            if x.end_with != "none":
                errors.append(f"сцена {x.n}: end_with работает только у последней сцены "
                              "группы — остальные кончаются внутренней склейкой")
    return errors


def _window_errors(p: ShotPlans, script: VideoScript, model: VideoModel) -> list[str]:
    """Что из клипа попадёт в ролик и есть ли там то, что задумано.

    Клип у модели не короче 5 с, сцена часто короче. Монтаж берёт начало клипа, а если
    у сцены переход в хвосте или с её последнего кадра стартует следующая — конец.
    Нужно и то и другое при длинном клипе — что-то одно потеряется: требуем выбрать.
    """
    errors: list[str] = []
    by_n = {s.n: s for s in script.shots}
    for plan in p.shots:
        shot = by_n.get(plan.n)
        if shot is None:
            continue
        if not shot.has_product and plan.start_from == "product_photo":
            errors.append(f"сцена {plan.n}: в ней нет товара (story_scene) — start_from не "
                          "может быть product_photo: generated_scene или prev_last_frame")
        if len(p.group_of(plan.n)) > 1:
            continue                      # окна внутри группы режет код по таймкодам
        try:
            clip = model.clip_duration(shot.duration_sec)
        except ValueError:
            continue                      # про слишком длинную сцену скажет режиссёр
        if (p.needs_head(plan.n) and p.needs_tail(plan.n)
                and clip - shot.duration_sec > PACING_GAP):
            errors.append(f"сцена {plan.n}: начинается выходом из перехода предыдущей сцены и "
                          f"сама нужна монтажу с конца (переход в хвосте или продолжение). "
                          f"Из клипа {clip} с в ролик войдут только {shot.duration_sec:g} с — "
                          "оба края не поместятся. Убери один из переходов")
    return errors


def check_plans(p: ShotPlans, script: VideoScript, model: VideoModel) -> list[str]:
    numbers = [s.n for s in p.shots]
    expected = [s.n for s in script.shots]
    if numbers != expected:
        return [f"сцены должны быть {expected}, а пришли {numbers}"]

    errors: list[str] = []
    if not p.style_anchor.strip():
        errors.append("style_anchor пустой — нужен общий стиль для всех сцен")
    if p.shots and p.shots[0].group_with_prev:
        errors.append("сцена 1 не может продолжать группу — она первая")
    errors += _group_errors(p, script, model)
    errors += _window_errors(p, script, model)
    run = 0
    last = len(p.shots) - 1
    for i, plan in enumerate(p.shots):
        nxt = p.shots[i + 1] if i < last else None
        text = plan.description.lower()
        full = build_prompt([plan], p.style_anchor)
        if len(full) > model.max_prompt_chars:
            errors.append(f"сцена {plan.n}: промпт {len(full)} символов, "
                          f"максимум {model.max_prompt_chars}")
        if _latin_share(plan.description + plan.preserve) < LATIN_SHARE:
            errors.append(f"сцена {plan.n}: prompt и preserve пиши на английском")
        if not plan.preserve.strip():
            errors.append(f"сцена {plan.n}: preserve пустой — назови, что обязано остаться "
                          "неизменным (товар, его форма, цвет, упаковка)")
        for word in BANNED_IN_PROMPT:
            if word in text:
                errors.append(f"сцена {plan.n}: «{word}» в описании — это один непрерывный кадр")
        for word in BANNED_GRAPHICS:
            if word in text:
                errors.append(f"сцена {plan.n}: «{word}» — надписи и логотипы кладём оверлеем "
                              "на монтаже, модель рисует их нечитаемо")
        if any(w in text for w in SPEECH_WORDS) and CLOSED_LIPS not in text:
            errors.append(f"сцена {plan.n}: говорящий человек не совпадёт с нашей озвучкой. "
                          f"Убери речь или добавь «{CLOSED_LIPS}»")
        if not CAMERA_RE.search(text):
            errors.append(f"сцена {plan.n}: опиши движение камеры словарём модели "
                          "(push in / pull out / arc / tracking / static ...) "
                          "с амплитудой и скоростью")
        if any(w in plan.soundscape.lower() for w in MUSIC_WORDS):
            errors.append(f"сцена {plan.n}: музыку у модели не заказываем — она своя в каждой "
                          "генерации. В soundscape только звуки в кадре")

        if plan.group_with_prev:
            pass                      # это та же генерация, отдельного стыка нет
        elif plan.start_from == "prev_last_frame":
            run += 1
            if i == 0:
                errors.append("сцена 1 не может продолжать предыдущую — до неё ничего нет")
            elif not model.supports_chain:
                errors.append(f"{model.title} не умеет продолжать предыдущую сцену")
            if run > MAX_CHAIN_RUN:
                errors.append(f"сцена {plan.n}: подряд больше {MAX_CHAIN_RUN} продолжений — "
                              "ролик превратится в один план и накопит искажения, "
                              "поставь новую сцену с фото товара")
            if plan.chain == "reference" and not model.chain_model:
                errors.append(f"{model.title}: нет модели для продолжения по хвосту видео — "
                              "chain должен быть frame")
        else:
            run = 0
            if plan.chain != "frame":
                errors.append(f"сцена {plan.n}: chain = reference только для продолжения "
                              "предыдущей сцены")
        if plan.end_with != "none" and not model.supports_end_frame:
            errors.append(f"{model.title} не умеет конечный кадр — end_with должен быть none")
        if plan.end_with == "next_start" and i == last:
            errors.append("у последней сцены end_with не может быть next_start")
        if plan.transition == "morph" and plan.end_with != "next_start":
            errors.append(f"сцена {plan.n}: morph работает только вместе с end_with = next_start")
        if i == last and plan.transition != "cut":
            errors.append("у последней сцены transition = cut")
        errors += _pair_errors(plan, nxt)

    if len(p.references) > MAX_REFERENCE_CHAINS:
        errors.append(f"бесшовных продолжений (chain = reference) не больше "
                      f"{MAX_REFERENCE_CHAINS} на ролик — оставь их главному повороту, "
                      f"сейчас {len(p.references)}: {p.references}")
    return errors


def _shots_block(script: VideoScript, model: VideoModel) -> str:
    rows = []
    for s in script.shots:
        ref = f" · фото {s.photo_ref}" if s.photo_ref else ""
        star = " ⭐главная" if s.n == script.key_shot else ""
        who = f" · в кадре: {', '.join(s.cast)}" if s.cast else ""
        try:
            length = model.clip_duration(s.duration_sec)
            clip = f" · в ролике {s.duration_sec:g} с из клипа ≈{length} с"
        except ValueError:
            clip = ""
        rows.append(f"{s.n} · {s.duration_sec} с · {s.role}{star} · {s.shot_size} · "
                    f"{s.location or 'место не указано'}{who}\n"
                    f"   кадр: {s.visual}\n   событие: {s.event or '—'}\n"
                    f"   камера: {s.camera} · источник: {s.source}{ref}{clip}"
                    f" · стык по сценарию: {s.transition}")
    return "\n".join(rows)


def _cast_block(script: VideoScript) -> str:
    if not script.cast:
        return "Героев нет."
    return "\n".join(f"- {c.name}: {c.look}" for c in script.cast)


async def plan_shots(llm: LLMProvider, brief: Brief, direction: Direction, script: VideoScript,
                     model: VideoModel, *, instruction: str = "") -> ShotPlans:
    chain_note = ("Можно: chain = \"reference\" — бесшовное продолжение по куску прошлого "
                  f"клипа, не больше {MAX_REFERENCE_CHAINS} на ролик."
                  if model.chain_model else
                  "Продолжение только со стоп-кадра: chain всегда \"frame\".")
    system = prompts.render("shot_prompts", model_title=model.title,
                            model_guide=model.prompt_guide or "",
                            shots=_shots_block(script, model), cast=_cast_block(script),
                            direction=direction_block(direction),
                            playbook=playbook.block("coverage", "transitions", "model_can",
                                                    "model_cannot", "lessons"),
                            chain_note=chain_note, max_chain=MAX_CHAIN_RUN)
    user = brief_message(brief, instruction or "Составь промпты и план непрерывности.")
    res = await parse_checked(llm, system, user, ShotPlans,
                              lambda p: check_plans(p, script, model), max_tokens=8000)
    res.prompt_version = prompts.load("shot_prompts").version
    return res


def plans_instruction(kind: str, current: ShotPlans | None, text: str = "") -> str:
    cur = current.model_dump_json(indent=1, exclude={"prompt_version"}) if current else ""
    match kind:
        case "edit":
            return (f"ПРАВКА ВЛАДЕЛЬЦА: {text}\nИзмени только то, о чём он просит. "
                    f"Текущий план:\n{cur}")
        case "variant":
            return ("Предложи ДРУГОЙ вариант: другие ракурсы, другое движение камеры, "
                    f"другой стиль картинки. Текущий план:\n{cur}")
    return ""
