from __future__ import annotations

from dataclasses import dataclass

from app import dedupe


@dataclass
class Item:
    text: str
    views: int


def test_normalize_strips_urls_mentions_and_punctuation():
    got = dedupe.normalize("Привет, МИР! https://t.me/x @channel — ёж 42")
    assert got == "привет мир еж 42"


def test_identical_texts_hash_identically():
    assert dedupe.simhash("одна и та же новость сегодня") == dedupe.simhash(
        "одна и та же новость сегодня"
    )


def test_unrelated_texts_are_far_apart():
    a = dedupe.simhash("В Берлине автомобиль въехал в толпу на параде")
    b = dedupe.simhash("Вышел Python 3.15 с новым JIT-компилятором и ускорением")
    assert dedupe.distance(a, b) > 3


def test_verbatim_repost_collapses_to_the_widest_reach():
    items = [
        Item("Совет директоров одобрил сделку на 4 миллиарда долларов", 100),
        Item("Совет директоров одобрил сделку на 4 миллиарда долларов", 900),
        Item("Совсем другая новость про погоду в Ростовской области", 50),
    ]
    kept, absorbed = dedupe.dedupe(items, lambda i: i.text, lambda i: i.views)

    assert len(kept) == 2
    winner = next(i for i in kept if "сделку" in i.text)
    assert winner.views == 900  # the copy with the larger audience survives
    assert absorbed  # and it records what it swallowed


def test_absorbed_indices_point_back_at_the_original_list():
    items = [Item("одинаковый текст новости", 10), Item("одинаковый текст новости", 20)]
    kept, absorbed = dedupe.dedupe(items, lambda i: i.text, lambda i: i.views)

    assert len(kept) == 1
    position, dropped = next(iter(absorbed.items()))
    assert kept[position].views == 20
    assert items[dropped[0]].views == 10


def test_media_only_posts_are_never_treated_as_duplicates():
    items = [Item("", 5), Item("", 7), Item("", 9)]
    kept, absorbed = dedupe.dedupe(items, lambda i: i.text, lambda i: i.views)

    assert len(kept) == 3
    assert absorbed == {}


def test_single_item_is_returned_untouched():
    items = [Item("что-нибудь", 1)]
    kept, absorbed = dedupe.dedupe(items, lambda i: i.text, lambda i: i.views)
    assert kept == items
    assert absorbed == {}


def test_any_match_is_case_and_punctuation_insensitive():
    assert dedupe.any_match("Купите КРИПТУ, сегодня!", ["крипта"]) is False
    assert dedupe.any_match("Купите КРИПТУ сегодня", ["крипту"]) is True
    assert dedupe.any_match("Обычный текст", ["реклама"]) is False
    assert dedupe.any_match("Обычный текст", []) is False
