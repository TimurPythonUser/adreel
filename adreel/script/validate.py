"""Правила поверх ответа модели. Промпт просит, код проверяет.

Ошибки возвращаются списком строк: они же уходят модели на переделку, поэтому
написаны так, чтобы было понятно, что исправить.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import pairwise

from ..product.schema import ProductCard
from .schema import Directions, VideoScript, Voiceover

#: доля от «темп × секунды»: диктору нужно дыхание, склейке — запас
VOICE_FILL = 0.92
#: хук: промпт просит до 3 с, проверка пропускает до 3.5 — переделка ради полсекунды
#: стоит дороже, чем она даёт. В тексте ошибки — цель из промпта, а не допуск.
HOOK_SEC = 3
HOOK_MAX_SEC = 3.5
#: слова-обещания, которые нельзя, если их нет в карточке дословно
RISKY = ("№1", "номер один", "номер 1", "100%", "гарант", "лучш", "найкращ", "best", "#1",
         "лечит", "вылеч", "лікує", "cure")

# Урок 22.10.2026: сценарии «товар крупно, наезд, капли» давали ролик-ожившую фотографию.
# Разнообразие держим проверками — промпт просит, код не пропускает.
#: «оживает фото товара» — максимум столько сцен; остальное — товар в деле и мир вокруг
MAX_PACKSHOTS = 1
#: людей в ролике — толпу и сложное взаимодействие модель не держит
MAX_CAST = 2
#: событие сцены — не движение камеры: «медленный наезд на кружку» ничего не показывает
CAMERA_ONLY = re.compile(
    r"(?:(?:медленн|плавн|быстр|лёгк|легк|камер|крупн)\w*\s+)*"
    r"(?:наезд|отъезд|облёт|облет|приближение|отдаление|панорама|зум|дуга|план)"
    r"(?:\s+(?:на|к|вокруг|от|по)\s+[^,.;]+)?\.?", re.IGNORECASE)
SIZE_TITLES = {"wide": "общий", "medium": "средний", "close": "крупный", "macro": "деталь"}


def check_directions(d: Directions, count: int) -> list[str]:
    titles = [x.title.strip().lower() for x in d.directions]
    errors: list[str] = []
    if len(d.directions) < min(count, 3):
        errors.append(f"нужно {count} направлений, а пришло {len(d.directions)}")
    if len(set(titles)) != len(titles):
        errors.append("направления повторяются — сделай их разными")
    for x in d.directions:
        if len([b for b in x.beats if b.strip()]) < 3:
            errors.append(f"«{x.title}»: распиши сюжет в beats — 3–5 событий, что ПРОИСХОДИТ "
                          "в кадре на каждом шаге")
        if not x.cast.strip():
            errors.append(f"«{x.title}»: заполни cast — кто в кадре (герой, руки) или "
                          "«без людей»")
        if not x.world.strip():
            errors.append(f"«{x.title}»: заполни world — где это происходит и какие предметы")
    return errors


def _story_errors(s: VideoScript) -> list[str]:
    """Ролик должен быть историей с событиями, а не набором планов товара."""
    errors: list[str] = []
    shots = s.shots
    names = {c.name for c in s.cast}
    if len(s.cast) > MAX_CAST:
        errors.append(f"героев {len(s.cast)} — максимум {MAX_CAST}: больше людей модель "
                      "не удержит")
    for c in s.cast:
        if not c.look.strip():
            errors.append(f"герой «{c.name}»: опиши внешность в look — возраст, волосы, одежда")
    events: dict[str, int] = {}
    for shot in shots:
        ev = shot.event.strip()
        if not ev:
            errors.append(f"кадр {shot.n}: заполни event — что ПРОИСХОДИТ в кадре за эти "
                          "секунды (действие героя, предмета или среды)")
        elif CAMERA_ONLY.fullmatch(ev):
            errors.append(f"кадр {shot.n}: «{ev}» — это движение камеры, а не событие. "
                          "Что меняется в кадре: кто что делает, что появляется, падает, "
                          "открывается?")
        elif ev.lower() in events:
            errors.append(f"кадры {events[ev.lower()]} и {shot.n}: одно и то же событие — "
                          "каждая сцена должна продвигать историю")
        else:
            events[ev.lower()] = shot.n
        for name in shot.cast:
            if name not in names:
                errors.append(f"кадр {shot.n}: героя «{name}» нет в cast — добавь его туда "
                              "с внешностью или убери из кадра")
    packshots = [x.n for x in shots if x.source == "product_photo"]
    if len(packshots) > MAX_PACKSHOTS:
        errors.append(f"кадры {packshots}: «оживает фото товара» — максимум {MAX_PACKSHOTS}. "
                      "Это даёт ролик из одной ожившей фотографии. Покажи товар в деле "
                      "(generated_scene: руки, человек, место) или мир вокруг (story_scene)")
    if len(shots) >= 4 and not any(not x.has_product or x.cast for x in shots):
        errors.append("в ролике только товар: добавь человека, его руки или его мир — "
                      "проблему, ситуацию, реакцию (story_scene или герой в cast)")
    sizes = [x.shot_size for x in shots]
    need = 3 if len(shots) >= 5 else 2
    if len(shots) >= 3 and len(set(sizes)) < need:
        got = ", ".join(SIZE_TITLES[z] for z in dict.fromkeys(sizes))
        errors.append(f"крупности: {got} — нужно хотя бы {need} разных (общий, средний, "
                      "крупный, деталь): одинаковые планы подряд читаются как один кадр")
    for a, b in pairwise(shots):
        same_place = a.location.strip().lower() == b.location.strip().lower() != ""
        if same_place and a.shot_size == b.shot_size and set(a.cast) == set(b.cast):
            errors.append(f"кадры {a.n} и {b.n}: тот же {SIZE_TITLES[a.shot_size]} план "
                          f"в том же месте ({a.location}) — склейка будет выглядеть скачком. "
                          "Смени крупность или ракурс")
    return errors


@dataclass
class ScriptRules:
    duration: int
    photo_ids: set[int] = field(default_factory=set)
    min_shots: int = 3
    max_shots: int = 7
    min_shot: float = 1.5
    max_shot: float = 8.0
    tolerance: float = 0.5

    @classmethod
    def for_duration(cls, duration: int, photo_ids: set[int], model_max: int) -> ScriptRules:
        max_shots = {15: 5, 20: 6}.get(duration, 7)
        return cls(duration=duration, photo_ids=photo_ids, min_shots=3, max_shots=max_shots,
                   max_shot=float(min(8, model_max)))


def normalize_script(s: VideoScript, r: ScriptRules) -> VideoScript:
    """Дешёвые исправления без повторного вызова модели: номера по порядку и
    небольшой перебор/недобор секунд — за счёт самого длинного кадра."""
    for i, shot in enumerate(s.shots, start=1):
        if shot.n != i:
            if s.key_shot == shot.n:
                s.key_shot = i
            shot.n = i
        shot.duration_sec = round(shot.duration_sec, 2)
        if shot.source == "story_scene":
            shot.photo_ref = None             # товара в сцене нет — и фото ему не нужно
        shot.cast = [c.strip() for c in shot.cast if c.strip()]
    diff = r.duration - s.total
    if 0 < abs(diff) <= 1.5:
        longest = max(s.shots, key=lambda x: x.duration_sec)
        new = round(longest.duration_sec + diff, 2)
        if r.min_shot <= new <= r.max_shot:
            longest.duration_sec = new
    return s


def check_script(s: VideoScript, r: ScriptRules) -> list[str]:
    errors: list[str] = []
    if abs(s.total - r.duration) > r.tolerance:
        errors.append(f"сумма длительностей кадров {s.total} с, а должна быть {r.duration} с")
    if not r.min_shots <= len(s.shots) <= r.max_shots:
        errors.append(f"кадров {len(s.shots)}, нужно от {r.min_shots} до {r.max_shots}")
    for shot in s.shots:
        if not r.min_shot <= shot.duration_sec <= r.max_shot:
            errors.append(f"кадр {shot.n}: {shot.duration_sec} с, допустимо "
                          f"{r.min_shot}–{r.max_shot} с")
        if shot.has_product and r.photo_ids and shot.photo_ref not in r.photo_ids:
            errors.append(f"кадр {shot.n}: photo_ref={shot.photo_ref} — такого фото нет, "
                          f"выбери из {sorted(r.photo_ids)}")
        words = len(shot.on_screen_text.split())
        if words > 7:
            errors.append(f"кадр {shot.n}: надпись {words} слов, максимум 7")
    if s.shots:
        first, last = s.shots[0], s.shots[-1]
        if first.role != "hook":
            errors.append(f"кадр 1 должен быть role=hook, а не {first.role}")
        if first.duration_sec > HOOK_MAX_SEC:
            errors.append(f"кадр 1 (хук) длится {first.duration_sec:g} с — сделай не длиннее "
                          f"{HOOK_SEC} с, остаток отдай следующим кадрам")
        if last.role not in ("cta", "offer"):
            errors.append("последний кадр должен быть role=cta или offer")
        if len(s.shots) >= 3 and not 1 < s.key_shot < len(s.shots):
            errors.append(f"key_shot={s.key_shot}: главный кадр не может быть первым или "
                          f"последним, выбери из 2–{len(s.shots) - 1}")
        key = next((x for x in s.shots if x.n == s.key_shot), None)
        if key is not None and not key.has_product:
            errors.append(f"key_shot={s.key_shot}: в главном кадре товар раскрывается — "
                          "он не может быть story_scene")
    errors += _story_errors(s)
    return errors


def char_limit(duration: float, chars_per_sec: float) -> int:
    return int(duration * chars_per_sec * VOICE_FILL)


def _card_sources(card: ProductCard) -> set[str]:
    return {f.source for f in card.benefits + card.specs} | {"user", "card"}


def check_voiceover(v: Voiceover, s: VideoScript, card: ProductCard,
                    chars_per_sec: float) -> list[str]:
    errors: list[str] = []
    by_n = {line.n: line for line in v.lines}
    missing = [shot.n for shot in s.shots if shot.n not in by_n]
    if missing:
        errors.append(f"нет строк озвучки для кадров {missing} (пустой text — это пауза)")
    for shot in s.shots:
        line = by_n.get(shot.n)
        if line is None:
            continue
        limit = char_limit(shot.duration_sec, chars_per_sec)
        if len(line.text) > limit:
            errors.append(f"кадр {shot.n}: {len(line.text)} символов, влезает {limit} — сократи")
    if not v.full_text.strip():
        errors.append("озвучка пустая")

    card_text = card.model_dump_json().lower()
    text = v.full_text.lower()
    for word in RISKY:
        if word in text and word not in card_text:
            errors.append(f"«{word}» — такого обещания нет в карточке, убери")
    for banned in card.forbidden_claims:
        c = banned.lower().strip()
        if c and c in text:
            errors.append(f"«{banned}» — в списке «нельзя обещать», убери")
    sources = _card_sources(card)
    for claim in v.claims:
        base = claim.source.strip()
        if base not in sources and not re.fullmatch(r"(page|photo|voice|text|link|frame):\d+",
                                                     base):
            errors.append(f"утверждение «{claim.text}»: источник «{claim.source}» не из карточки")
    return errors
