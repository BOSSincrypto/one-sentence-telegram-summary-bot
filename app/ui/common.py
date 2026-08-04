"""Shared building blocks for the admin interface."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

PER_PAGE = 8
Button = InlineKeyboardButton


class OwnerOnly(BaseMiddleware):
    """Single-owner bot: everyone else is politely turned away.

    ``owners`` is mutable so the router tree, which is built once per process,
    can have its allow-list refreshed without being rebuilt.
    """

    def __init__(self, owner_ids: Iterable[int]) -> None:
        self.owners = frozenset(owner_ids)

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]):
        user = data.get("event_from_user")
        if user is not None and user.id not in self.owners:
            if isinstance(event, CallbackQuery):
                await event.answer("Доступ закрыт", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("Этот бот приватный.")
            return None
        return await handler(event, data)


def kb(*rows: Sequence[Button]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(row) for row in rows if row])


def back(target: str = "m", label: str = "⬅️ Назад") -> Button:
    return Button(text=label, callback_data=target)


def pager(prefix: str, page: int, total_pages: int) -> list[Button]:
    """``prefix`` is completed with the page number, e.g. ``"ch|p|"``."""
    if total_pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(Button(text="◀️", callback_data=f"{prefix}{page - 1}"))
    row.append(Button(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        row.append(Button(text="▶️", callback_data=f"{prefix}{page + 1}"))
    return row


def page_slice(items: Sequence[Any], page: int, per: int = PER_PAGE) -> tuple[list[Any], int, int]:
    total_pages = max(1, -(-len(items) // per))
    page = max(0, min(page, total_pages - 1))
    return list(items[page * per : (page + 1) * per]), page, total_pages


def cut(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def show(
    event: Message | CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    *,
    alert: str | None = None,
) -> None:
    """Edits the message a button lives on, or sends a fresh one."""
    if isinstance(event, CallbackQuery):
        if alert:
            await event.answer(alert, show_alert=True)
        else:
            await event.answer()
        if event.message is None:
            return
        try:
            await event.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                raise
        return
    await event.answer(text, reply_markup=markup)


def on_off(value: Any) -> str:
    return "вкл" if value else "выкл"


def yes_no(value: Any) -> str:
    return "✅" if value else "❌"
