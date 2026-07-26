from __future__ import annotations

import time

import pytest

from app import db, digest, tme
from app.llm import Item, LLMError, Result


class FakeOpenRouter:
    """Stands in for the model: echoes a deterministic summary per post."""

    def __init__(
        self, *, fail: bool = False, omit: set[str] | None = None, ads: set[str] | None = None
    ):
        self.fail = fail
        self.omit = omit or set()
        self.ads = ads or set()
        self.calls: list[tuple[str, list[Item]]] = []

    async def summarize(self, channel_title, items, chain):
        self.calls.append((channel_title, list(items)))
        if self.fail:
            raise LLMError("модель недоступна")
        results = {
            item.key: Result(summary=f"Кратко: {item.text[:30]}.", is_ad=item.text in self.ads)
            for item in items
            if item.text not in self.omit
        }
        return results, 0.001, "fake/model"


def post(pid: int, text: str, *, views: int = 0, ago: int = 60, media: bool = False) -> tme.Post:
    return tme.Post(
        id=pid,
        ts=int(time.time()) - ago,
        text=text,
        views=views,
        has_media=media,
        channel="chan",
    )


def make_group(channels: list[str] = ("chan",), **fields) -> tuple[int, dict[str, int]]:
    ids = {name: db.add_channel(name) for name in channels}
    group_id = db.add_group("Тест")
    for channel_id in ids.values():
        db.toggle_group_channel(group_id, channel_id)
    if fields:
        db.update_group(group_id, **fields)
    return group_id, ids


def patch_fetch(monkeypatch, by_channel: dict[str, list[tme.Post]]):
    async def fake_fetch(session, username, since, max_pages=8):
        posts = [p for p in by_channel.get(username, []) if p.ts >= since]
        return f"Канал {username}", posts

    monkeypatch.setattr(digest.tme, "fetch_posts", fake_fetch)


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        "Отличный курс со скидкой, промокод LETO",
        "#реклама партнёрский материал",
        "Наш партнёрский материал о путешествиях",
        "erid: 2Vfnxw",
    ],
)
def test_ad_heuristics_catch_common_markers(text):
    assert digest.looks_like_ad(text) is True


def test_ad_heuristics_leave_ordinary_news_alone():
    assert digest.looks_like_ad("Совет директоров одобрил сделку") is False


def test_first_sentence_truncates_on_a_word_boundary():
    long = "Очень длинное предложение без точки которое надо аккуратно обрезать по слову"
    got = digest.first_sentence(long, 30)
    assert len(got) <= 31
    assert got.endswith("…")
    assert not got[:-1].endswith(" ")


def test_first_sentence_prefers_a_real_sentence_break():
    assert digest.first_sentence("Первое. Второе.", 100) == "Первое."


def test_cache_key_ignores_formatting_but_not_mode():
    a = digest.cache_key("Привет,  мир!", full=True, limit=160)
    b = digest.cache_key("привет мир", full=True, limit=160)
    c = digest.cache_key("Привет,  мир!", full=False, limit=160)
    assert a == b
    assert a != c


# --------------------------------------------------------------------------- #
# pipeline
# --------------------------------------------------------------------------- #


async def test_build_summarises_one_batch_per_channel(monkeypatch):
    db.put("short_verbatim", 0)  # force every post through the model
    group_id, _ = make_group(["chan", "other"])
    patch_fetch(
        monkeypatch,
        {
            "chan": [post(1, "Первая длинная новость про экономику страны")],
            "other": [post(2, "Вторая длинная новость про технологии и науку")],
        },
    )
    client = FakeOpenRouter()

    result = await digest.build(None, client, db.group(group_id))

    assert len(client.calls) == 2  # one request per channel, never per post
    assert result.total == 2
    assert result.cost == pytest.approx(0.002)
    assert all(line.summary for block in result.blocks for line in block.items)


async def test_media_only_posts_are_skipped_by_default(monkeypatch):
    group_id, _ = make_group()
    patch_fetch(
        monkeypatch, {"chan": [post(1, "", media=True), post(2, "Настоящая новость про город")]}
    )

    result = await digest.build(None, FakeOpenRouter(), db.group(group_id))

    assert [line.post_id for line in result.blocks[0].items] == [2]


async def test_media_only_posts_are_kept_without_an_llm_call_when_enabled(monkeypatch):
    db.put("skip_media_only", False)
    group_id, _ = make_group()
    patch_fetch(monkeypatch, {"chan": [post(1, "", media=True)]})
    client = FakeOpenRouter()

    result = await digest.build(None, client, db.group(group_id))

    assert client.calls == []
    assert result.blocks[0].items[0].summary == "Медиа без подписи."


async def test_short_posts_bypass_the_model_entirely(monkeypatch):
    db.put("short_verbatim", 80)
    group_id, _ = make_group()
    patch_fetch(monkeypatch, {"chan": [post(1, "Коротко и ясно.")]})
    client = FakeOpenRouter()

    result = await digest.build(None, client, db.group(group_id))

    assert client.calls == []
    assert result.blocks[0].items[0].summary == "Коротко и ясно."


async def test_obvious_ads_never_reach_the_model(monkeypatch):
    group_id, _ = make_group()
    patch_fetch(
        monkeypatch,
        {
            "chan": [
                post(1, "Купите курс по промокоду SUMMER со скидкой прямо сейчас"),
                post(2, "Обычная новость про городской транспорт и его развитие"),
            ]
        },
    )
    client = FakeOpenRouter()

    result = await digest.build(None, client, db.group(group_id))

    sent_texts = [item.text for _, items in client.calls for item in items]
    assert all("промокод" not in text for text in sent_texts)
    assert [line.post_id for line in result.blocks[0].items] == [2]


async def test_top_posts_are_detailed_and_the_rest_collapse(monkeypatch):
    db.put("max_posts", 2)
    db.put("overflow_max", 2)
    group_id, _ = make_group()
    patch_fetch(
        monkeypatch,
        {
            "chan": [
                post(i, f"Новость номер {i} с достаточно длинным текстом", views=i * 10)
                for i in range(1, 6)
            ]
        },
    )

    result = await digest.build(None, FakeOpenRouter(), db.group(group_id))
    block = result.blocks[0]

    assert {line.post_id for line in block.items} == {5, 4}  # highest reach
    assert {line.post_id for line in block.extra} == {3, 2}
    assert block.hidden == 1
    assert all(line.full for line in block.items)
    assert not any(line.full for line in block.extra)


async def test_cross_channel_duplicates_collapse_to_one_line(monkeypatch):
    group_id, _ = make_group(["chan", "other"])
    shared = "Землетрясение магнитудой шесть произошло у побережья острова сегодня утром"
    patch_fetch(
        monkeypatch,
        {
            "chan": [post(1, shared, views=10)],
            "other": [tme.Post(2, int(time.time()) - 60, shared, 900, False, "other")],
        },
    )

    result = await digest.build(None, FakeOpenRouter(), db.group(group_id))

    assert result.deduped == 1
    assert result.total == 1
    survivor = result.blocks[0].items[0]
    assert survivor.views == 900
    assert survivor.also_in  # names the channel it displaced


async def test_already_sent_posts_are_not_repeated(monkeypatch):
    group_id, ids = make_group()
    patch_fetch(monkeypatch, {"chan": [post(1, "Новость которую уже отправляли ранее сегодня")]})
    db.mark_sent(group_id, [(ids["chan"], 1)])

    result = await digest.build(None, FakeOpenRouter(), db.group(group_id))

    assert result.blocks == []


async def test_preview_ignores_the_sent_ledger(monkeypatch):
    group_id, ids = make_group()
    patch_fetch(monkeypatch, {"chan": [post(1, "Новость которую уже отправляли ранее сегодня")]})
    db.mark_sent(group_id, [(ids["chan"], 1)])

    result = await digest.build(None, FakeOpenRouter(), db.group(group_id), respect_sent=False)

    assert result.total == 1


async def test_summaries_are_cached_across_runs(monkeypatch):
    db.put("short_verbatim", 0)
    group_id, _ = make_group()
    patch_fetch(monkeypatch, {"chan": [post(1, "Новость которая попадёт в кэш саммари надолго")]})

    first = FakeOpenRouter()
    await digest.build(None, first, db.group(group_id), respect_sent=False)
    second = FakeOpenRouter()
    result = await digest.build(None, second, db.group(group_id), respect_sent=False)

    assert len(first.calls) == 1
    assert second.calls == []  # served from cache, costs nothing
    assert result.cost == 0.0


async def test_model_failure_degrades_to_a_trimmed_first_sentence(monkeypatch):
    db.put("short_verbatim", 0)
    group_id, _ = make_group()
    patch_fetch(monkeypatch, {"chan": [post(1, "Первое предложение новости. Второе предложение.")]})

    result = await digest.build(None, FakeOpenRouter(fail=True), db.group(group_id))

    assert result.blocks[0].items[0].summary == "Первое предложение новости."
    assert any("модель недоступна" in err for err in result.errors)


async def test_omitted_items_still_get_a_summary(monkeypatch):
    db.put("short_verbatim", 0)
    text = "Модель забыла про эту новость но строка всё равно должна быть"
    group_id, _ = make_group()
    patch_fetch(monkeypatch, {"chan": [post(1, text)]})

    result = await digest.build(None, FakeOpenRouter(omit={text}), db.group(group_id))

    assert result.blocks[0].items[0].summary


async def test_keyword_blacklist_filters_before_the_model(monkeypatch):
    db.put("short_verbatim", 0)
    group_id, _ = make_group(kw_deny='["криптовалюта"]')
    patch_fetch(
        monkeypatch,
        {
            "chan": [
                post(1, "Очередная криптовалюта выросла на сорок процентов за сутки"),
                post(2, "Городской совет утвердил новый бюджет на следующий год"),
            ]
        },
    )
    client = FakeOpenRouter()

    result = await digest.build(None, client, db.group(group_id))

    assert [line.post_id for line in result.blocks[0].items] == [2]
    assert len(client.calls[0][1]) == 1


async def test_keyword_whitelist_keeps_only_matching_posts(monkeypatch):
    group_id, _ = make_group(kw_allow='["бюджет"]')
    patch_fetch(
        monkeypatch,
        {
            "chan": [
                post(1, "Городской совет утвердил новый бюджет на следующий год"),
                post(2, "Совсем посторонняя новость о погоде и дождях в регионе"),
            ]
        },
    )

    result = await digest.build(None, FakeOpenRouter(), db.group(group_id))

    assert [line.post_id for line in result.blocks[0].items] == [1]


async def test_channel_failure_is_reported_without_killing_the_digest(monkeypatch):
    group_id, ids = make_group(["chan", "broken"])

    async def fake_fetch(session, username, since, max_pages=8):
        if username == "broken":
            raise tme.ChannelError("предпросмотр отключён")
        return "Канал chan", [post(1, "Новость из работающего канала про экономику")]

    monkeypatch.setattr(digest.tme, "fetch_posts", fake_fetch)

    result = await digest.build(None, FakeOpenRouter(), db.group(group_id))

    assert result.total == 1
    assert any("broken" in err for err in result.errors)
    assert db.channel(ids["broken"])["fail_count"] == 1


async def test_group_model_override_keeps_the_global_chain_as_fallback(monkeypatch):
    db.put("models", ["global/a", "global/b"])
    db.put("short_verbatim", 0)
    group_id, _ = make_group(model="special/x")
    patch_fetch(monkeypatch, {"chan": [post(1, "Новость достаточной длины для вызова модели")]})

    seen: list[list[str]] = []

    class RecordingClient(FakeOpenRouter):
        async def summarize(self, channel_title, items, chain):
            seen.append(list(chain))
            return await super().summarize(channel_title, items, chain)

    await digest.build(None, RecordingClient(), db.group(group_id))

    assert seen == [["special/x", "global/a", "global/b"]]
