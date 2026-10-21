from __future__ import annotations

import re

from adreel.render.subtitles import (
    Caption,
    SubMode,
    SubtitleStyle,
    ass_escape,
    ass_time,
    build_ass,
    font_size_for,
    hex_to_ass,
    split_lines,
)
from adreel.tts.base import Word, words_from_chars


def test_scene_captions_go_on_top_with_own_style():
    """Надпись сцены — отдельным стилем сверху кадра: снизу субтитры голоса."""
    ass = build_ass([Word("Кава", 0.0, 0.4)], 1080, 1920, SubtitleStyle(),
                    [Caption(0.0, 2.5, "Гаряча  {12}\nгодин"), Caption(3.0, 3.0, "пусто")])
    cap_style = next(ln for ln in ass.splitlines() if ln.startswith("Style: Cap,"))
    assert cap_style.split(",")[18] == "8"                  # Alignment: верх по центру
    assert "Dialogue: 1,0:00:00.00,0:00:02.50,Cap,,0,0,0,,Гаряча (12) годин" in ass
    assert "пусто" not in ass                               # нулевая длительность — мимо
    assert "Kar" in ass                                     # субтитры голоса на месте


def test_captions_without_voice_still_build():
    ass = build_ass([], 1080, 1920, SubtitleStyle(), [Caption(0.0, 2.0, "−20%")])
    assert "Cap,,0,0,0,,−20%" in ass


def test_hex_to_ass_reverses_bytes():
    assert hex_to_ass("#FF3366") == "&H006633FF"
    assert hex_to_ass("#000000", alpha=0x80) == "&H80000000"


def test_ass_time():
    assert ass_time(0) == "0:00:00.00"
    assert ass_time(61.234) == "0:01:01.23"
    assert ass_time(-1) == "0:00:00.00"


def test_escape_blocks_tags():
    assert ass_escape("a{b}\\c") == "a(b)/c"


def test_words_from_chars_strips_stress_and_spaces():
    text = "Ка́ва  гаряча"
    chars = list(text)
    starts = [i * 0.1 for i in range(len(chars))]
    ends = [s + 0.1 for s in starts]
    words = words_from_chars(chars, starts, ends)
    assert [w.text for w in words] == ["Кава", "гаряча"]
    assert words[0].start == 0.0
    assert words[1].end == ends[-1]


def test_one_font_size_shrinks_only_for_longest_word():
    style = SubtitleStyle(font_size=96, min_font_size=40)
    assert font_size_for(["кава"], 1080, style) == 96
    small = font_size_for(["надзвичайнодовгеслововрекламі"], 1080, style)
    assert 40 <= small < 96


def test_split_lines_respects_max_words():
    words = [Word(f"w{i}", i, i + 0.5) for i in range(7)]
    rows = split_lines(words, 1080, 60, SubtitleStyle(max_words=3))
    assert [len(r) for r in rows] == [3, 3, 1]


def test_line_does_not_cross_phrase_end_or_pause():
    """«годин. Замовляй» — следующая фраза висела на экране раньше, чем её сказали."""
    words = [Word("тепло", 0.0, 0.4), Word("годин.", 0.5, 0.9), Word("Замовляй", 1.0, 1.5),
             Word("на", 1.6, 1.7), Word("сайті", 2.6, 3.0)]
    rows = split_lines(words, 1080, 60, SubtitleStyle(max_words=3))
    assert [[w.text for w in r] for r in rows] == [["тепло", "годин."], ["Замовляй", "на"],
                                                   ["сайті"]]


def test_karaoke_durations_cover_event():
    words = [Word("Кава", 0.0, 0.3), Word("знову", 0.4, 0.8), Word("холодна", 0.9, 1.5)]
    style = SubtitleStyle(font_size=60, max_words=3, mode=SubMode.KARAOKE)  # влезает в одну строку
    ass = build_ass(words, 1080, 1920, style)
    line = next(x for x in ass.splitlines() if x.startswith("Dialogue:"))
    assert ",0:00:00.00,0:00:01.50,Kar," in line
    ks = [int(k) for k in re.findall(r"\\kf(\d+)", line)]
    assert sum(ks) == 150          # подсветка ровно на длительность события


def test_alternate_mode_switches_style_per_phrase():
    words = [Word(f"w{i}", i, i + 0.5) for i in range(4)]
    ass = build_ass(words, 1080, 1920, SubtitleStyle(max_words=2, mode=SubMode.ALTERNATE))
    styles = [x.split(",")[3] for x in ass.splitlines() if x.startswith("Dialogue:")]
    assert styles == ["Ac", "Tx"]


def test_brand_colors_land_in_styles():
    ass = build_ass([Word("a", 0, 1)], 1080, 1920,
                    SubtitleStyle(text_color="#FFFFFF", accent_color="#FF3366"))
    assert "&H006633FF" in ass
