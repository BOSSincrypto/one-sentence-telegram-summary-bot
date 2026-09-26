from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from app import db, digest, render, runner, tme
from app.digest import CHANNEL_GROUP_ID
from app.llm import Item, Result, _parse

MSK = ZoneInfo("Europe/Moscow")


class FakeChannelRouter:
    """Fake OpenRouter that honours the channel label+rank style."""

    def __init__(self, ranks: dict[str, int] | None = None):
        self.ranks = ranks or {}
        self.calls: list[tuple[str, list[Item], list[str], str]] = []

    async def summarize(self, channel_title, items, chain, style="sentence"):
        self.calls.append((channel_title, list(items), list(chain), style))
        results = {}
        for item in items:
            rank = self.ranks.get(item.text, 3)
            results[item.key] = Result(summary=f"Ярлык {item.text[:20]}", is_ad=False, rank=rank)
        return results, 0.001, "fake/model"


def post(pid: int, text: str, *, views: int = 0, ago: int = 60) -> tme.Post:
    return tme.Post(
        id=pid, ts=int(time.time()) - ago, text=text, views=views, has_media=False, channel="chan"
    )


def patch_fetch(monkeypatch, by_channel: dict[str, list[tme.Post]]):
    async def fake_fetch(session, username, since, max_pages=8):
        posts = [p for p in by_channel.get(username, []) if p.ts >= since]
        return f"Канал {username}", posts

    monkeypatch.setattr(digest.tme, "fetch_posts", fake_fetch)


def seed_channels(*names: str) -> None:
    for name in names:
        db.add_channel(name)


# --------------------------------------------------------------------------- #
# llm rank parsing
# --------------------------------------------------------------------------- #


def test_parse_reads_rank_and_clamps_it():
    items = [Item("k1", "текст раз", True), Item("k2", "текст два", True)]
    raw = '{"items": [{"i": 1, "s": "Ярлык раз", "ad": false, "r": 5}, {"i": 2, "s": "Ярлык два", "ad": false, "r": 99}]}'
    out = _parse(raw, items)
    assert out["k1"].rank == 5
    assert out["k2"].rank == 5  # clamped


def test_parse_defaults_rank_to_3_for_old_payloads():
    items = [Item("k1", "текст", True)]
    out = _parse('{"items": [{"i": 1, "s": "Ярлык", "ad": false}]}', items)
    assert out["k1"].rank == 3


# --------------------------------------------------------------------------- #
# build_channel
# --------------------------------------------------------------------------- #


async def test_build_channel_uses_label_style_and_filters_by_rank(monkeypatch):
    db.put("short_verbatim", 0)
    db.put("channel_max_posts", 5)
    db.put("channel_total_max", 25)
    db.put("channel_min_rank", 3)
    seed_channels("chan", "other")
    patch_fetch(
        monkeypatch,
        {
            "chan": [post(1, "Важная новость про биткоин и рынок сегодня")],
            "other": [post(2, "Мусорный розыгрыш подпишись мусор мусор")],
        },
    )
    client = FakeChannelRouter(
        {
            "Важная новость про биткоин и рынок сегодня": 5,
            "Мусорный розыгрыш подпишись мусор мусор": 1,
        }
    )

    result = await digest.build_channel(None, client)

    assert result.group_id == CHANNEL_GROUP_ID
    assert result.group_name == "CRYPTO ВЫЖИМКА"
    assert result.total == 1
    assert all(call[3] == "label" for call in client.calls)
    assert len(client.calls) == 2  # still one request per channel, rank inside


async def test_build_channel_truncates_to_total_max(monkeypatch):
    db.put("short_verbatim", 0)
    db.put("channel_max_posts", 5)
    db.put("channel_total_max", 2)
    db.put("channel_min_rank", 1)
    seed_channels("chan")
    patch_fetch(
        monkeypatch,
        {"chan": [post(i, f"Длинная новость номер {i} про рынок и эфир") for i in range(1, 5)]},
    )
    result = await digest.build_channel(None, FakeChannelRouter())

    assert result.total == 2


async def test_build_channel_never_empty_when_posts_exist(monkeypatch):
    db.put("short_verbatim", 0)
    db.put("channel_min_rank", 5)
    seed_channels("chan")
    patch_fetch(monkeypatch, {"chan": [post(1, "Обычная длинная новость про рынок сегодня")]})
    result = await digest.build_channel(None, FakeChannelRouter({"x": 1}))

    assert result.total == 1  # rank fallback keeps the best of the rejected


# --------------------------------------------------------------------------- #
# render_channel
# --------------------------------------------------------------------------- #


def _line(pid: int, summary: str, title: str = "NFT RU", rank: int = 5):
    return digest.Line(
        channel_id=1,
        channel="nftru",
        channel_title=title,
        post_id=pid,
        ts=int(time.time()),
        views=100,
        text="x",
        full=True,
        summary=summary,
        rank=rank,
    )


def _digest(*lines: digest.Line) -> digest.Digest:
    now = int(time.time())
    block = digest.Block(channel_id=1, username="nftru", title="NFT RU", items=list(lines))
    return digest.Digest(
        group_id=0,
        group_name="CRYPTO ВЫЖИМКА",
        group_emoji="⚡",
        blocks=[block] if lines else [],
        window_start=now - 86400,
        window_end=now,
    )


def test_render_channel_looks_like_ciscrypted():
    out = render.render_channel(
        _digest(_line(6341, "У AscendEx проблемы с ликвидностью.")), "@cryptovyzhimka"
    )
    text = "\n".join(out)

    assert "CRYPTO ВЫЖИМКА" in text
    assert "22:00 МСК" in text
    assert (
        '<b>NFT RU:</b> <a href="https://t.me/nftru/6341">У AscendEx проблемы с ликвидностью</a>'
        in text
    )
    assert "Подпишись / поделись" in text
    assert "https://t.me/cryptovyzhimka" in text
    assert "Открыть" not in text  # flat style, no per-post open buttons


def test_render_channel_orders_by_rank_then_views():
    low = _line(1, "Слабая", rank=2)
    high = _line(2, "Топ", rank=5)
    text = "\n".join(render.render_channel(_digest(low, high), "@cryptovyzhimka"))

    assert text.index("Топ") < text.index("Слабая")


def test_render_channel_empty_day_publishes_stub():
    text = "\n".join(render.render_channel(_digest(), "@cryptovyzhimka"))

    assert "Сегодня тихо" in text


# --------------------------------------------------------------------------- #
# runner schedule + publish
# --------------------------------------------------------------------------- #


def at(hour: int, minute: int = 0, day: int = 26) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=MSK)


def test_channel_due_logic():
    db.put("channel_dest", "@cryptovyzhimka")
    db.put("channel_time", "22:00")

    assert runner.is_channel_due(at(21, 59)) is False
    assert runner.is_channel_due(at(22, 0)) is True

    db.put(runner.LAST_CHANNEL_DAY_KEY, "2026-07-26")
    assert runner.is_channel_due(at(22, 30)) is False
    assert runner.is_channel_due(at(22, 30, day=27)) is True


def test_channel_not_due_without_dest():
    db.put("channel_dest", "")
    assert runner.is_channel_due(at(23, 0)) is False


async def test_run_channel_publishes_and_stamps_day(monkeypatch):
    db.put("short_verbatim", 0)
    db.put("channel_dest", "@cryptovyzhimka")
    seed_channels("chan")
    patch_fetch(monkeypatch, {"chan": [post(1, "Важная длинная новость про рынок сегодня")]})

    sent: list[tuple] = []

    class FakeBot:
        async def send_message(self, chat_id, text, message_thread_id=None):
            sent.append((chat_id, text, message_thread_id))

    result = await runner.run_channel(FakeBot(), None, FakeChannelRouter())

    assert result is not None and result.total == 1
    assert sent and sent[0][0] == "@cryptovyzhimka"
    assert sent[0][2] is None  # channels take no thread
    assert db.already_sent(CHANNEL_GROUP_ID, [(1, 1)]) == {(1, 1)}
    assert db.get(runner.LAST_CHANNEL_DAY_KEY) != ""


async def test_run_all_runs_channel_with_no_groups(monkeypatch):
    db.put("short_verbatim", 0)
    db.put("channel_dest", "@cryptovyzhimka")
    db.put("channel_time", "00:00")
    db.put(runner.LAST_CHANNEL_DAY_KEY, "2000-01-01")
    seed_channels("chan")
    patch_fetch(monkeypatch, {"chan": [post(1, "Важная длинная новость про рынок сегодня")]})

    class FakeBot:
        async def send_message(self, chat_id, text, message_thread_id=None):
            pass

    results = await runner.run_all(FakeBot(), None, FakeChannelRouter())

    assert len(results) == 1
    assert results[0].group_id == CHANNEL_GROUP_ID
