"""Субтитры ASS из пословных таймингов озвучки.

Перенесено из AI_Host (render/compose.py) с двумя изменениями:
  • цвета, шрифт и отступы приходят из стиля (бренд-кит), а не зашиты под thepublic;
  • кроме «фраза целиком, цвета чередуются» есть караоке: фраза стоит целиком,
    а текущее слово подсвечивается по голосу (тег \\kf).

Сохранены проверенные решения прототипа:
  • ОДИН кегль на ролик — иначе размер «прыгает» от строки к строке;
  • кегль опускается только под самое длинное слово, остальное решает перенос строк;
  • фигурные скобки и бекслеш экранируются — в ASS они открывают теги.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..tts.base import Word

# Ширина глифа кириллицы жирным шрифтом в рендере libass, доля от кегля. Замерено
# в AI_Host на e-Ukraine Bold: в среднем 0.52, на широких словах до 0.58.
# Для кегля берём худший случай, для переноса строк — чуть выше среднего.
GLYPH_W = 0.58
GLYPH_W_LINE = 0.55
#: здесь фраза кончается — новая строка субтитров
SENTENCE_END = (".", "!", "?", "…")
#: пауза в голосе длиннее этого — тоже новая строка (стык сцен, вдох диктора)
MAX_GAP = 0.6


class SubMode(StrEnum):
    KARAOKE = "karaoke"        # фраза целиком, текущее слово подсвечено
    ALTERNATE = "alternate"    # фраза целиком, цвет чередуется пофразно


@dataclass(frozen=True)
class SubtitleStyle:
    font_name: str = "e-Ukraine Bold"
    font_size: int = 96          # базовый кегль при PlayRes = размер кадра
    min_font_size: int = 56
    text_color: str = "#FFFFFF"
    accent_color: str = "#FFD400"
    outline_color: str = "#202020"
    outline: int = 4
    shadow: int = 1
    max_words: int = 3
    margin_lr: int = 90          # поля строки слева/справа
    margin_word: int = 24        # поле, когда одно длинное слово занимает строку
    margin_v: int = 420          # отступ снизу (выше подписей и кнопок площадки)
    mode: SubMode = SubMode.KARAOKE
    caption_scale: float = 0.8   # надпись сцены мельче субтитров: она фон, а не голос
    caption_margin_v: int = 260  # отступ надписи сверху (ниже ника и шапки площадки)


@dataclass(frozen=True)
class Caption:
    """Надпись поверх видео на время сцены (on_screen_text сценария).

    Видеомодели текст не доверяем — буквы плывут. Надпись рисует libass, сверху кадра:
    снизу заняты субтитры голоса."""

    start: float
    end: float
    text: str


def hex_to_ass(color: str, alpha: int = 0) -> str:
    """#RRGGBB -> &HAABBGGRR (в ASS байты цвета идут в обратном порядке)."""
    c = color.lstrip("#")
    if len(c) != 6:
        raise ValueError(f"цвет должен быть #RRGGBB, а не {color!r}")
    r, g, b = c[0:2], c[2:4], c[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def ass_time(t: float) -> str:
    cs = round(max(0.0, t) * 100)
    return f"{cs // 360000}:{(cs // 6000) % 60:02d}:{(cs // 100) % 60:02d}.{cs % 100:02d}"


def ass_escape(text: str) -> str:
    """Фигурные скобки и бекслеш в ASS открывают теги — в тексте им не место."""
    return text.replace("\\", "/").replace("{", "(").replace("}", ")")


def font_size_for(words: list[str], width: int, style: SubtitleStyle) -> int:
    """Один кегль на весь ролик: базовый, но не больше, чем позволяет самое длинное слово."""
    longest = max((len(w) for w in words), default=0)
    if not longest:
        return style.font_size
    fit = int((width - 2 * style.margin_word) / (longest * GLYPH_W))
    return max(style.min_font_size, min(style.font_size, fit))


def split_lines(words: list[Word], width: int, size: int, style: SubtitleStyle) -> list[list[Word]]:
    """Экранные строки: до max_words слов и не шире кадра. Кегль не трогаем.

    Строка не переходит через конец фразы и паузу в голосе: иначе на экране висит
    «годин. Замовляй» — следующая фраза (часто уже другой сцены) видна до того,
    как её сказали.
    """
    avail = width - 2 * style.margin_lr
    out: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        trial = [*cur, w]
        text_w = len(" ".join(x.text for x in trial)) * GLYPH_W_LINE * size
        phrase_end = bool(cur) and (cur[-1].text.endswith(SENTENCE_END)
                                    or w.start - cur[-1].end > MAX_GAP)
        if cur and (phrase_end or len(trial) > style.max_words or text_w > avail):
            out.append(cur)
            cur = [w]
        else:
            cur = trial
    if cur:
        out.append(cur)
    return out


def _karaoke_text(row: list[Word]) -> str:
    """{\\kf N} — длительность подсветки слова в сантисекундах.

    Паузу между словами прибавляем к предыдущему слову, чтобы подсветка не убегала
    вперёд голоса: сумма \\kf строки равна длительности события.
    """
    parts = []
    for i, w in enumerate(row):
        end = row[i + 1].start if i + 1 < len(row) else w.end
        cs = max(1, round((end - w.start) * 100))
        parts.append(f"{{\\kf{cs}}}{ass_escape(w.text)}")
    return " ".join(parts)


def build_ass(words: list[Word], width: int, height: int, style: SubtitleStyle,
              captions: list[Caption] | None = None) -> str:
    captions = [c for c in captions or [] if c.text.strip() and c.end > c.start]
    size = font_size_for([w.text for w in words], width, style)
    cap_words = [w for c in captions for w in c.text.split()]
    cap_size = max(style.min_font_size,
                   round(font_size_for(cap_words, width, style) * style.caption_scale))
    text = hex_to_ass(style.text_color)
    accent = hex_to_ass(style.accent_color)
    outline = hex_to_ass(style.outline_color)
    back = hex_to_ass("#000000", alpha=0x80)
    # Bold=0: жирность даёт сам файл шрифта. С Bold=-1 libass берёт Regular и
    # раздувает его искусственно (урок из AI_Host).
    tail = (f"0,0,0,0,100,100,0,0,1,{style.outline},{style.shadow},2,"
            f"{style.margin_lr},{style.margin_lr},{style.margin_v},1")
    # Alignment 8 — верх по центру; MarginV тогда отступ от верхнего края
    cap_tail = (f"0,0,0,0,100,100,0,0,1,{style.outline},{style.shadow},8,"
                f"{style.margin_lr},{style.margin_lr},{style.caption_margin_v},1")
    fmt = ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
           "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
           "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding")
    header = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]", fmt,
        # Караоке: Primary — цвет ПОСЛЕ подсветки, Secondary — ДО. Поэтому уже
        # сказанные слова остаются акцентными, а впереди идёт белый текст.
        f"Style: Kar,{style.font_name},{size},{accent},{text},{outline},{back},{tail}",
        f"Style: Tx,{style.font_name},{size},{text},{text},{outline},{back},{tail}",
        f"Style: Ac,{style.font_name},{size},{accent},{accent},{outline},{back},{tail}",
        f"Style: Cap,{style.font_name},{cap_size},{text},{text},{outline},{back},{cap_tail}",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    events = [f"Dialogue: 1,{ass_time(c.start)},{ass_time(c.end)},Cap,,0,0,0,,"
              f"{ass_escape(' '.join(c.text.split()))}" for c in captions]
    for n, row in enumerate(split_lines(words, width, size, style)):
        plain = " ".join(ass_escape(w.text) for w in row)
        wide = len(plain) * GLYPH_W_LINE * size > width - 2 * style.margin_lr
        m = style.margin_word if wide else 0
        if style.mode is SubMode.KARAOKE:
            name, body = "Kar", _karaoke_text(row)
        else:
            name, body = ("Ac" if n % 2 == 0 else "Tx"), plain
        start, end = ass_time(row[0].start), ass_time(row[-1].end)
        events.append(f"Dialogue: 0,{start},{end},{name},,{m},{m},0,,{body}")
    return "\n".join(header + events) + "\n"
