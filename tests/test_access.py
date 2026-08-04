"""End-to-end checks of the owner gate.

The middleware is attached to the private-chat router, and its handlers live in
nested routers. Whether aiogram runs a parent router's middleware for a nested
router's handler is exactly the kind of assumption that must not be taken on
trust in a bot that is supposed to be private, so these tests push real Update
objects through a real Dispatcher.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import Chat, Message, Update, User

from app import db, ui
from app.fsm import SqliteStorage

OWNER = 111
STRANGER = 222
TOKEN = "424242:AAHtestTokenForUnitTestsOnly0000000"


class MockSession(BaseSession):
    """Records outbound API calls instead of performing them."""

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


@pytest.fixture(scope="module")
def dispatcher():
    """One Dispatcher for the module: aiogram routers are single-parent."""
    dp = Dispatcher(storage=SqliteStorage())
    dp.include_router(ui.build_router({OWNER}))
    dp.workflow_data.update(session=None, openrouter=None, config=None, owner_ids={OWNER})
    return dp


@pytest.fixture
def harness(dispatcher):
    session = MockSession()
    bot = Bot(TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    return bot, dispatcher, session


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


async def test_owner_reaches_the_menu(harness):
    bot, dp, session = harness

    await dp.feed_update(bot, private_message(OWNER, "/menu"))

    assert any("Дайджест-бот" in text for text in session.texts())


async def test_stranger_is_turned_away_and_sees_no_settings(harness):
    bot, dp, session = harness

    await dp.feed_update(bot, private_message(STRANGER, "/menu"))

    texts = session.texts()
    assert texts == ["Этот бот приватный."]
    assert not any("Дайджест-бот" in text for text in texts)


async def test_stranger_cannot_delete_a_channel(harness):
    """The gate has to hold on callbacks that mutate state, not just on /menu."""
    bot, dp, session = harness
    channel_id = db.add_channel("victim")

    await dp.feed_update(
        bot,
        Update(
            update_id=7,
            callback_query={
                "id": "cb-del",
                "from": {"id": STRANGER, "is_bot": False, "first_name": "T"},
                "chat_instance": "x",
                "data": f"ch|d|{channel_id}",
                "message": {
                    "message_id": 8,
                    "date": int(datetime.now(UTC).timestamp()),
                    "chat": {"id": STRANGER, "type": "private"},
                    "text": "…",
                },
            },
        ),
    )

    assert db.channel(channel_id) is not None  # survived
    assert session.texts() == ["Доступ закрыт"]


async def test_stranger_callback_is_rejected(harness):
    bot, dp, session = harness
    update = Update(
        update_id=3,
        callback_query={
            "id": "cb1",
            "from": {"id": STRANGER, "is_bot": False, "first_name": "T"},
            "chat_instance": "x",
            "data": "ch",
            "message": {
                "message_id": 5,
                "date": int(datetime.now(UTC).timestamp()),
                "chat": {"id": STRANGER, "type": "private"},
                "text": "…",
            },
        },
    )

    await dp.feed_update(bot, update)

    answers = [c for c in session.calls if isinstance(c, AnswerCallbackQuery)]
    assert len(answers) == 1
    assert answers[0].text == "Доступ закрыт"
    assert answers[0].show_alert is True
    assert not any(isinstance(c, EditMessageText) for c in session.calls)


async def test_owner_callback_opens_the_channel_screen(harness):
    bot, dp, session = harness
    update = Update(
        update_id=4,
        callback_query={
            "id": "cb2",
            "from": {"id": OWNER, "is_bot": False, "first_name": "T"},
            "chat_instance": "x",
            "data": "ch",
            "message": {
                "message_id": 6,
                "date": int(datetime.now(UTC).timestamp()),
                "chat": {"id": OWNER, "type": "private"},
                "text": "…",
            },
        },
    )

    await dp.feed_update(bot, update)

    edits = [c for c in session.calls if isinstance(c, EditMessageText)]
    assert edits and "Каналы" in edits[0].text


async def test_conversation_state_survives_a_restart(harness):
    """FSM lives in SQLite precisely so a sleeping Machine does not lose it."""
    bot, dp, _ = harness
    start = Update(
        update_id=9,
        callback_query={
            "id": "cb3",
            "from": {"id": OWNER, "is_bot": False, "first_name": "T"},
            "chat_instance": "x",
            "data": "ch|add",
            "message": {
                "message_id": 7,
                "date": int(datetime.now(UTC).timestamp()),
                "chat": {"id": OWNER, "type": "private"},
                "text": "…",
            },
        },
    )
    await dp.feed_update(bot, start)

    # A brand-new storage object stands in for the process coming back from
    # sleep: nothing is held in memory, so it must read the state back.
    reborn = SqliteStorage()
    state = await reborn.get_state(StorageKey(bot_id=bot.id, chat_id=OWNER, user_id=OWNER))

    assert state == "AddChannel:waiting"
