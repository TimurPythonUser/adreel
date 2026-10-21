from __future__ import annotations

from adreel.product.questions import (
    MAX_QUESTIONS,
    NO_LIMITS,
    NO_OFFER,
    SKIP,
    apply_answer,
    parse_deadline,
    pending_questions,
)
from adreel.product.schema import Fact, ProductCard, Suggestions


def full_card() -> ProductCard:
    return ProductCard(
        name="Кружка", what_it_is="Термокружка", audience="офисные", price="899 грн",
        benefits=[Fact(text=t, source="page:1") for t in ("тепло 8 ч", "не течёт", "лёгкая")],
        forbidden_claims=["лучшая"], tone="дружелюбный", cta="Заказывай",
        answered=["offer"],
    )


def test_nothing_to_ask_for_full_card():
    assert pending_questions(full_card()) == []


def test_empty_card_asks_in_order_and_caps():
    qs = pending_questions(ProductCard(), Suggestions(tone=["смешной"]))
    assert len(qs) == MAX_QUESTIONS
    assert [q.field for q in qs][:3] == ["name", "what_it_is", "audience"]
    tone_q = next(q for q in pending_questions(full_card().model_copy(update={"tone": ""})))
    assert tone_q.field == "tone"


def test_answered_fields_are_not_asked_again():
    card = full_card().model_copy(update={"price": ""})
    assert [q.field for q in pending_questions(card)] == ["price"]
    card = apply_answer(card, "price", SKIP, year=2026)
    assert pending_questions(card) == []


def test_benefits_answer_appends_user_facts_without_duplicates():
    card = ProductCard(benefits=[Fact(text="тепло 8 ч", source="page:1")])
    qs = pending_questions(card, Suggestions(benefits=["тепло 8 ч", "не течёт"]))
    q = next(q for q in qs if q.field == "benefits")
    assert q.options == ["не течёт"]                 # уже известный плюс не предлагаем
    card = apply_answer(card, "benefits", "не течёт\n• лёгкая\nтепло 8 ч", year=2026)
    assert [(b.text, b.source) for b in card.benefits] == [
        ("тепло 8 ч", "page:1"), ("не течёт", "user"), ("лёгкая", "user")]


def test_offer_with_deadline_and_no_offer():
    card = apply_answer(ProductCard(), "offer", "−20% до 20.09", year=2026)
    assert card.offer is not None and card.offer.ends_at == "2026-09-20"
    card = apply_answer(card, "offer", NO_OFFER, year=2026)
    assert card.offer is None and "offer" in card.answered


def test_forbidden_no_limits_keeps_list_empty_but_answered():
    card = apply_answer(ProductCard(), "forbidden_claims", NO_LIMITS, year=2026)
    assert card.forbidden_claims == [] and "forbidden_claims" in card.answered


def test_parse_deadline_variants():
    assert parse_deadline("до 5/10/27", 2026) == "2027-10-05"
    assert parse_deadline("скидка без срока", 2026) is None
    assert parse_deadline("до 45.13", 2026) is None
