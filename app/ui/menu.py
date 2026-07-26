"""Main menu and the manual run action."""

from __future__ import annotations

from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiohttp import ClientSession

from .. import db, render, runner
from ..llm import OpenRouter
from .common import Button, cut, kb, show

router = Router(name="menu")


def menu_text() -> str:
    channels = db.channels()
    active = sum(1 for c in channels if c["enabled"])
    groups = db.groups()
    bound = sum(1 for g in groups if g["enabled"] and g["chat_id"])
    chain = list(db.get("models") or [])
    zone = render.tz()
    upcoming = runner.next_run_at()
    today = datetime.now(zone).date()
    when = "сегодня" if upcoming.date() == today else upcoming.strftime("%d.%m")
    spent = db.month_cost(db.local_date().strftime("%Y-%m"))

    lines = [
        "🤖 <b>Дайджест-бот</b>",
        "",
        f"📡 Каналы: {len(channels)} (активных {active})",
        f"🗂 Группы: {len(groups)} (настроено {bound})",
        f"🧠 Модель: {cut(chain[0], 40) if chain else '<b>не выбрана</b>'}",
        f"🕘 Ближайший дайджест: {when} в {upcoming:%H:%M} ({db.get('tz')})",
        f"💰 Потрачено в этом месяце: ${spent:.4f}",
    ]
    if not chain:
        lines += ["", "⚠️ Выберите модель в разделе «ИИ» — без неё дайджест не соберётся."]
    if not bound:
        lines += ["", "⚠️ Ни одна группа не привязана к чату — некуда отправлять."]
    return "\n".join(lines)


def menu_kb():
    return kb(
        [Button(text="📡 Каналы", callback_data="ch"), Button(text="🗂 Группы", callback_data="g")],
        [Button(text="🧠 ИИ", callback_data="ai"), Button(text="⚙️ Настройки", callback_data="s")],
        [
            Button(text="📊 Статистика", callback_data="st"),
            Button(text="▶️ Собрать сейчас", callback_data="run"),
        ],
        [Button(text="🔄 Обновить", callback_data="m")],
    )


@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(menu_text(), reply_markup=menu_kb())


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    thread = message.message_thread_id
    await message.answer(
        f"chat_id: <code>{message.chat.id}</code>\n"
        f"thread_id: <code>{thread or '—'}</code>\n"
        f"your id: <code>{message.from_user.id if message.from_user else '—'}</code>"
    )


@router.callback_query(F.data == "m")
async def open_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await show(call, menu_text(), menu_kb())


@router.callback_query(F.data == "noop")
async def noop(call: CallbackQuery) -> None:
    await call.answer()


@router.callback_query(F.data == "run")
async def run_now(
    call: CallbackQuery, bot: Bot, session: ClientSession, openrouter: OpenRouter
) -> None:
    groups = db.groups(enabled_only=True)
    if not groups:
        await call.answer("Нет активных групп с указанным получателем.", show_alert=True)
        return

    await show(call, "⏳ Собираю дайджест…")
    results = []
    for group in groups:
        results.append(await runner.run_group(bot, session, openrouter, group))
    db.put(runner.LAST_DAY_KEY, datetime.now(render.tz()).date().isoformat())
    db.prune()

    lines = ["✅ <b>Готово</b>", ""]
    for result in results:
        status = "отправлено" if result.blocks else "нечего отправлять"
        lines.append(
            f"• <b>{render.esc(result.group_name)}</b>: {result.total} постов, "
            f"${result.cost:.4f} — {status}"
        )
        for err in result.errors[:3]:
            lines.append(f"   ⚠️ {render.esc(err)}")
    await show(call, "\n".join(lines), menu_kb())
