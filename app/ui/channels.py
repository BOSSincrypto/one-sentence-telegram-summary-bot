"""Channel management: add (with validation), enable/disable, delete."""

from __future__ import annotations

from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiohttp import ClientSession

from .. import db, render, tme
from .common import Button, back, cut, kb, page_slice, pager, show

router = Router(name="channels")
MAX_BATCH = 20


class AddChannel(StatesGroup):
    waiting = State()


def _list_screen(page: int) -> tuple[str, object]:
    channels = db.channels()
    rows, page, total_pages = page_slice(channels, page)

    buttons = [
        [
            Button(
                text=f"{'✅' if row['enabled'] else '⏸'} @{cut(row['username'], 20)}"
                + (f" · {cut(row['title'], 18)}" if row["title"] else ""),
                callback_data=f"ch|v|{row['id']}",
            )
        ]
        for row in rows
    ]
    nav = pager("ch|p|", page, total_pages)
    text = (
        f"📡 <b>Каналы</b> — {len(channels)}\n\n"
        "Бот читает публичный веб-превью канала (t.me/s/…), поэтому подписка и "
        "права администратора не нужны — достаточно, чтобы у канала был включён "
        "публичный доступ."
    )
    if not channels:
        text += "\n\n<i>Пока пусто. Добавьте первый канал.</i>"
    return text, kb(*buttons, nav, [Button(text="➕ Добавить", callback_data="ch|add"), back()])


@router.callback_query(F.data == "ch")
@router.callback_query(F.data.startswith("ch|p|"))
async def channel_list(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    page = int(call.data.rsplit("|", 1)[1]) if call.data.startswith("ch|p|") else 0
    text, markup = _list_screen(page)
    await show(call, text, markup)


@router.callback_query(F.data.startswith("ch|v|"))
async def channel_view(call: CallbackQuery) -> None:
    channel_id = int(call.data.rsplit("|", 1)[1])
    row = db.channel(channel_id)
    if row is None:
        await show(call, *_list_screen(0), alert="Канал уже удалён")
        return

    used_in = db.groups_using_channel(channel_id)
    checked = (
        datetime.fromtimestamp(row["last_ok_at"], render.tz()).strftime("%d.%m %H:%M")
        if row["last_ok_at"]
        else "ещё не читался"
    )
    lines = [
        f"📡 <b>{render.esc(row['title'] or row['username'])}</b>",
        f'@{render.esc(row["username"])} · <a href="https://t.me/s/{row["username"]}">превью</a>',
        "",
        f"Статус: {'активен' if row['enabled'] else 'выключен'}",
        f"В группах: {render.esc(', '.join(used_in)) if used_in else '—'}",
        f"Последнее успешное чтение: {checked}",
    ]
    if row["last_error"]:
        lines.append(f"\n⚠️ {render.esc(row['last_error'])} (подряд: {row['fail_count']})")
    if not used_in:
        lines.append("\n<i>Канал не входит ни в одну группу — он не попадёт в дайджест.</i>")

    await show(
        call,
        "\n".join(lines),
        kb(
            [
                Button(
                    text="⏸ Выключить" if row["enabled"] else "▶️ Включить",
                    callback_data=f"ch|t|{channel_id}",
                ),
                Button(text="🗑 Удалить", callback_data=f"ch|d|{channel_id}"),
            ],
            [back("ch", "⬅️ К списку")],
        ),
    )


@router.callback_query(F.data.startswith("ch|t|"))
async def channel_toggle(call: CallbackQuery) -> None:
    channel_id = int(call.data.rsplit("|", 1)[1])
    row = db.channel(channel_id)
    if row is not None:
        db.set_channel_enabled(channel_id, not row["enabled"])
    call.data = f"ch|v|{channel_id}"
    await channel_view(call)


@router.callback_query(F.data.startswith("ch|d|"))
async def channel_delete(call: CallbackQuery) -> None:
    channel_id = int(call.data.rsplit("|", 1)[1])
    db.delete_channel(channel_id)
    text, markup = _list_screen(0)
    await show(call, text, markup, alert="Канал удалён")


@router.callback_query(F.data == "ch|add")
async def channel_add_prompt(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddChannel.waiting)
    await show(
        call,
        "➕ <b>Добавление каналов</b>\n\n"
        "Пришлите один или несколько каналов — через пробел, запятую или с новой "
        "строки. Подойдёт любой формат:\n"
        "<code>@meduzalive</code>, <code>t.me/rian_ru</code>, "
        "<code>https://t.me/s/bbcrussian</code>\n\n"
        f"За один раз — до {MAX_BATCH} штук. Каждый будет проверен.",
        kb([back("ch", "⬅️ Отмена")]),
    )


@router.message(AddChannel.waiting)
async def channel_add(message: Message, state: FSMContext, session: ClientSession) -> None:
    raw = (message.text or "").replace(",", " ").split()
    if not raw:
        await message.answer("Не вижу ни одного канала. Пришлите @username или ссылку.")
        return

    await state.clear()
    status = await message.answer("⏳ Проверяю…")

    added, failed, existing = [], [], []
    known = {row["username"] for row in db.channels()}
    for item in raw[:MAX_BATCH]:
        try:
            username = tme.normalize_username(item)
        except tme.ChannelError as exc:
            failed.append(f"{render.esc(item)} — {render.esc(str(exc))}")
            continue
        if username in known:
            existing.append(username)
            continue
        try:
            title = await tme.probe(session, username)
        except tme.ChannelError as exc:
            failed.append(f"@{username} — {render.esc(str(exc))}")
            continue
        db.add_channel(username, title)
        known.add(username)
        added.append(f"@{username} — {render.esc(title)}")

    lines = []
    if added:
        lines.append("✅ <b>Добавлены</b>\n" + "\n".join(f"• {x}" for x in added))
    if existing:
        lines.append("ℹ️ <b>Уже были</b>: " + ", ".join(f"@{x}" for x in existing))
    if failed:
        lines.append("⚠️ <b>Не удалось</b>\n" + "\n".join(f"• {x}" for x in failed))
    if len(raw) > MAX_BATCH:
        lines.append(f"<i>Обработаны первые {MAX_BATCH} из {len(raw)}.</i>")
    if added:
        lines.append("\n<i>Не забудьте добавить их в группу — иначе они не попадут в дайджест.</i>")

    await status.edit_text(
        "\n\n".join(lines) or "Ничего не добавлено.",
        reply_markup=kb([Button(text="📡 К каналам", callback_data="ch"), back()]),
    )
