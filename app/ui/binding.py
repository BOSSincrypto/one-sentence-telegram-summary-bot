"""Discovering forum topics to deliver into.

The Bot API has no "list forum topics" method, so topics have to be learned.
Two sources are used, in order of how little traffic they cost:

1. service messages (``forum_topic_created``) and anything else Telegram hands
   the bot anyway — these arrive even with privacy mode on, which keeps the
   sleeping machine from being woken by ordinary group chatter;
2. an explicit ``/bind`` in the target topic, which always works, including for
   topics that already existed before the bot joined.
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import ChatMemberUpdated, Message

from .. import db
from .common import Button, kb

router = Router(name="binding")
GROUPS = {ChatType.GROUP, ChatType.SUPERGROUP}


def _remember(message: Message) -> tuple[int, int]:
    chat = message.chat
    thread_id = message.message_thread_id or 0
    title = ""
    if message.forum_topic_created:
        title = message.forum_topic_created.name
    elif thread_id == 0:
        title = "General"
    else:
        existing = db.topic(chat.id, thread_id)
        title = existing["title"] if existing else f"Тема #{thread_id}"
    db.remember_topic(chat.id, thread_id, title, chat.title or str(chat.id))
    return chat.id, thread_id


@router.message(F.chat.type.in_(GROUPS), Command("bind"))
async def bind(message: Message, bot: Bot, owner_ids: frozenset[int]) -> None:
    user = message.from_user
    if user is None or user.id not in owner_ids:
        return

    chat_id, thread_id = _remember(message)
    label = db.topic(chat_id, thread_id)
    name = f"{message.chat.title} › {label['title'] if label else thread_id}"

    try:
        await message.delete()
    except Exception:
        pass  # no delete rights: leaving the command in place is harmless

    await bot.send_message(
        user.id,
        f"📍 Тема запомнена:\n<b>{name}</b>\n\n"
        f"chat_id <code>{chat_id}</code> · thread_id <code>{thread_id or '—'}</code>\n\n"
        "Теперь выберите её в группе каналов: «Куда слать».",
        reply_markup=kb([Button(text="🗂 К группам", callback_data="g")]),
    )


@router.message(F.chat.type.in_(GROUPS), F.forum_topic_created)
async def topic_created(message: Message) -> None:
    _remember(message)


@router.message(F.chat.type.in_(GROUPS))
async def observe(message: Message) -> None:
    """Records whatever topic activity Telegram chooses to deliver."""
    _remember(message)


@router.my_chat_member(F.chat.type.in_(GROUPS))
async def joined(event: ChatMemberUpdated, bot: Bot, owner_ids: frozenset[int]) -> None:
    status = event.new_chat_member.status
    chat = event.chat
    if status in {"left", "kicked"}:
        return

    db.remember_topic(chat.id, 0, "General", chat.title or str(chat.id))
    for owner in owner_ids:
        try:
            await bot.send_message(
                owner,
                f"➕ Бот добавлен в <b>{chat.title}</b>.\n\n"
                "Чтобы отправлять дайджест в конкретную тему, напишите в ней "
                "<code>/bind</code> — бот запомнит её и удалит своё сообщение.",
                reply_markup=kb([Button(text="🗂 К группам", callback_data="g")]),
            )
        except Exception:
            pass
