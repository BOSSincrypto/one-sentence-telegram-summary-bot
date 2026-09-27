"""End-to-end checks of the public-channel screen.

Pushes real Updates through a real Dispatcher with a recording Bot session:
menu entry, status screen, dest/time/number wizards, source picker, preview
and manual publishing.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app import db, digest, runner, tme, ui
from app.fsm import SqliteStorage
from app.llm import Result

OWNER = 111
TOKEN = "424242:AAHtestTokenForUnitTestsOnly0000000"


class MockSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list = []

    async def close(self) -> None:
        return None

    async def stream_content(self, *args, **kwargs):  # pragma: no cover - unused
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            return Message(
                message_id=1,
                date=datetime.now(UTC),
                chat=Chat(id=getattr(method, "chat_id", OWNER), type="private"),
                text=getattr(method, "text", ""),
            )
        return True

    def texts(self) -> list[str]:
        out = []
        for call in self.calls:
            if isinstance(call, (SendMessage, EditMessageText)):
                out.append(call.text)
            elif isinstance(call, AnswerCallbackQuery):
                out.append(call.text or "")
        return out


class FakeChannelRouter:
    async def summarize(self, channel_title, items, chain, style="sentence"):
        return (
            {item.key: Result(summary="Ярлык новости", is_ad=False, rank=5) for item in items},
            0.001,
            "fake/model",
        )


@pytest.fixture
def harness():
    # Feature routers are module-level singletons that aiogram binds to one
    # parent: detach them so this module can assemble its own tree even when
    # another test module already built one.
    for mod in (
        ui.ai,
        ui.binding,
        ui.channel,
        ui.channels,
        ui.groups,
        ui.menu,
        ui.settings,
        ui.stats,
    ):
        mod.router._parent_router = None
    ui._root, ui._gate = None, None  # fresh router tree per test
    session = MockSession()
    bot = Bot(TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=SqliteStorage())
    dp.include_router(ui.build_router({OWNER}))
    dp.workflow_data.update(
        session=object(),
        openrouter=FakeChannelRouter(),
        config=SimpleNamespace(cronjob_key="", tick_url="http://x/tick"),
        owner_ids={OWNER},
    )
    return bot, dp, session


def private_message(user_id: int, text: str, update_id: int = 1) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(UTC),
            chat=Chat(id=user_id, type="private"),
            from_user=User(id=user_id, is_bot=False, first_name="T"),
            text=text,
        ),
    )


def callback(user_id: int, data: str, update_id: int = 2) -> Update:
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=f"cb{update_id}",
            from_user=User(id=user_id, is_bot=False, first_name="T"),
            chat_instance="1",
            message=Message(
                message_id=update_id,
                date=datetime.now(UTC),
                chat=Chat(id=user_id, type="private"),
                text="menu",
            ),
            data=data,
        ),
    )


def patch_fetch(monkeypatch, by_channel: dict[str, list[tme.Post]]):
    async def fake_fetch(session, username, since, max_pages=8):
        posts = [p for p in by_channel.get(username, []) if p.ts >= since]
        return f"Канал {username}", posts

    monkeypatch.setattr(digest.tme, "fetch_posts", fake_fetch)


async def test_menu_shows_channel_entry(harness):
    bot, dp, session = harness

    await dp.feed_update(bot, private_message(OWNER, "/menu"))

    assert any("📣 Канал" in text for text in session.texts())


async def test_channel_screen_and_toggle(harness):
    bot, dp, session = harness

    await dp.feed_update(bot, callback(OWNER, "pub"))
    assert any("Публичный канал" in text for text in session.texts())

    await dp.feed_update(bot, callback(OWNER, "pub|toggle", update_id=3))
    assert db.get("channel_enabled") is False
    await dp.feed_update(bot, callback(OWNER, "pub|toggle", update_id=4))
    assert db.get("channel_enabled") is True


async def test_dest_wizard_accepts_and_rejects(harness):
    bot, dp, _ = harness

    await dp.feed_update(bot, callback(OWNER, "pub|dest"))
    await dp.feed_update(bot, private_message(OWNER, "not a channel!!", update_id=10))
    assert runner.channel_dest() == ""

    await dp.feed_update(bot, private_message(OWNER, "@cryptovyzhimka", update_id=11))
    assert runner.channel_dest() == "@cryptovyzhimka"


async def test_dest_wizard_accepts_numeric_id(harness):
    bot, dp, _ = harness

    await dp.feed_update(bot, callback(OWNER, "pub|dest"))
    await dp.feed_update(bot, private_message(OWNER, "-100123456", update_id=10))

    assert runner.channel_dest() == "-100123456"


async def test_time_wizard(harness):
    bot, dp, _ = harness

    await dp.feed_update(bot, callback(OWNER, "pub|time"))
    await dp.feed_update(bot, private_message(OWNER, "полночь", update_id=10))
    assert db.get("channel_time") == "22:00"  # default untouched

    await dp.feed_update(bot, private_message(OWNER, "21:30", update_id=11))
    assert db.get("channel_time") == "21:30"


async def test_number_wizard(harness):
    bot, dp, _ = harness

    await dp.feed_update(bot, callback(OWNER, "pub|n|channel_min_rank"))
    await dp.feed_update(bot, private_message(OWNER, "99", update_id=10))
    assert db.get("channel_min_rank") == 3

    await dp.feed_update(bot, private_message(OWNER, "4", update_id=11))
    assert db.get("channel_min_rank") == 4


async def test_source_picker(harness):
    bot, dp, session = harness
    db.add_channel("chan")
    gid = db.add_group("AI")
    db.toggle_group_channel(gid, db.add_channel("other"))

    await dp.feed_update(bot, callback(OWNER, "pub|src"))
    assert any("Источники выпуска" in text for text in session.texts())

    await dp.feed_update(bot, callback(OWNER, f"pub|src|{gid}", update_id=3))
    assert db.get("channel_group_id") == gid

    await dp.feed_update(bot, callback(OWNER, "pub|src", update_id=4))
    await dp.feed_update(bot, callback(OWNER, "pub|src|0", update_id=5))
    assert db.get("channel_group_id") == 0


async def test_custom_set_select_autofills_and_toggles(harness):
    from app.ui import channel as channel_ui

    bot, dp, session = harness
    c1 = db.add_channel("aaa")
    c2 = db.add_channel("bbb")

    await dp.feed_update(bot, callback(OWNER, "pub|src|custom"))
    assert db.get("channel_group_id") == channel_ui.CHANNEL_SOURCE_CUSTOM
    assert db.pub_source_ids() == {c1, c2}  # starts as today's enabled set

    await dp.feed_update(bot, callback(OWNER, "pub|set|0", update_id=3))
    assert any("Набор канала" in text for text in session.texts())

    await dp.feed_update(bot, callback(OWNER, f"pub|sett|{c1}|0", update_id=4))
    assert db.pub_source_ids() == {c2}

    await dp.feed_update(bot, callback(OWNER, "pub", update_id=5))
    assert any("набор канала (1 акт.)" in text for text in session.texts())


async def test_preview_sends_to_owner_without_stamping_day(harness, monkeypatch):
    bot, dp, session = harness
    db.put("short_verbatim", 0)
    db.put("channel_dest", "@cryptovyzhimka")
    db.add_channel("chan")
    patch_fetch(
        monkeypatch,
        {
            "chan": [
                tme.Post(
                    id=1,
                    ts=int(time.time()) - 60,
                    text="Длинная новость про рынок биткоина сегодня",
                    views=10,
                    has_media=False,
                    channel="chan",
                )
            ]
        },
    )

    await dp.feed_update(bot, callback(OWNER, "pub|preview"))

    assert any("Предпросмотр отправлен" in text for text in session.texts())
    assert db.get(runner.LAST_CHANNEL_DAY_KEY) != db.local_date().isoformat()


async def test_publish_now_posts_and_stamps_day(harness, monkeypatch):
    bot, dp, session = harness
    db.put("short_verbatim", 0)
    db.put("channel_dest", "@cryptovyzhimka")
    db.add_channel("chan")
    patch_fetch(
        monkeypatch,
        {
            "chan": [
                tme.Post(
                    id=7,
                    ts=int(time.time()) - 60,
                    text="Длинная новость про рынок биткоина сегодня",
                    views=10,
                    has_media=False,
                    channel="chan",
                )
            ]
        },
    )

    sent = []
    real_send = runner.send_messages

    async def spy(bot_, chat_id, thread_id, messages):
        sent.append(chat_id)
        return await real_send(bot_, 111, None, messages)

    monkeypatch.setattr(runner, "send_messages", spy)

    await dp.feed_update(bot, callback(OWNER, "pub|now"))

    assert sent == ["@cryptovyzhimka"]
    assert any("Выпуск опубликован" in text for text in session.texts())
    assert db.get(runner.LAST_CHANNEL_DAY_KEY) == db.local_date().isoformat()
