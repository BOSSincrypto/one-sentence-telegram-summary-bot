"""Groups: a set of channels, a destination topic, and per-group overrides."""

from __future__ import annotations

import json

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiohttp import ClientSession

from .. import db, render, runner
from ..llm import OpenRouter
from .common import Button, back, cut, kb, page_slice, pager, show

router = Router(name="groups")


class GroupName(StatesGroup):
    creating = State()
    renaming = State()


class GroupField(StatesGroup):
    limit = State()
    keywords = State()


def _kw(raw: str) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [str(x) for x in value] if isinstance(value, list) else []


def _dest_label(group) -> str:
    if not group["chat_id"]:
        return "не задан"
    name = group["dest_name"] or str(group["chat_id"])
    return name


def _list_screen(page: int) -> tuple[str, object]:
    groups = db.groups()
    rows, page, total_pages = page_slice(groups, page)
    buttons = [
        [
            Button(
                text=f"{'✅' if row['enabled'] and row['chat_id'] else '⏸'} "
                f"{row['emoji']}{cut(row['name'], 24)} · "
                f"{len(db.group_channel_ids(int(row['id'])))} кан.",
                callback_data=f"g|v|{row['id']}",
            )
        ]
        for row in rows
    ]
    text = (
        f"🗂 <b>Группы</b> — {len(groups)}\n\n"
        "Группа — это набор каналов и адрес, куда уходит их общий дайджест "
        "(тема в супергруппе или личка)."
    )
    if not groups:
        text += "\n\n<i>Пока пусто. Создайте первую группу.</i>"
    return text, kb(
        *buttons,
        pager("g|p|", page, total_pages),
        [Button(text="➕ Новая группа", callback_data="g|new"), back()],
    )


@router.callback_query(F.data == "g")
@router.callback_query(F.data.startswith("g|p|"))
async def group_list(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    page = int(call.data.rsplit("|", 1)[1]) if call.data.startswith("g|p|") else 0
    await show(call, *_list_screen(page))


@router.callback_query(F.data.startswith("g|v|"))
async def group_view(call: CallbackQuery) -> None:
    group_id = int(call.data.rsplit("|", 1)[1])
    group = db.group(group_id)
    if group is None:
        await show(call, *_list_screen(0), alert="Группа удалена")
        return

    channels = db.group_channels(group_id)
    allow, deny = _kw(group["kw_allow"]), _kw(group["kw_deny"])
    limit = group["max_posts"] or db.get("max_posts")
    lines = [
        f"🗂 <b>{group['emoji']}{render.esc(group['name'])}</b>",
        "",
        f"📍 Куда: {render.esc(_dest_label(group))}",
        f"📡 Каналов: {len(channels)}"
        + (
            f" ({render.esc(cut(', '.join('@' + c['username'] for c in channels), 90))})"
            if channels
            else ""
        ),
        f"🧠 Модель: {render.esc(cut(group['model'], 40)) if group['model'] else 'общая цепочка'}",
        f"🔢 Постов на канал: {limit}" + ("" if group["max_posts"] else " (общая настройка)"),
        "🔍 Ключевые слова: "
        + (f"только с {len(allow)}" if allow else "без белого списка")
        + (f", кроме {len(deny)}" if deny else ""),
        f"⚙️ Статус: {'активна' if group['enabled'] else 'выключена'}",
    ]
    if not group["chat_id"]:
        lines.append(
            "\n⚠️ Получатель не задан. Добавьте бота в супергруппу, напишите "
            "<code>/bind</code> в нужной теме — и она появится в списке."
        )
    if not channels:
        lines.append("\n⚠️ В группе нет каналов.")

    await show(
        call,
        "\n".join(lines),
        kb(
            [
                Button(text="📡 Каналы группы", callback_data=f"g|ch|{group_id}|0"),
                Button(text="📍 Куда слать", callback_data=f"g|dst|{group_id}|0"),
            ],
            [
                Button(text="🧠 Модель", callback_data=f"g|mdl|{group_id}"),
                Button(text="🔢 Лимит", callback_data=f"g|lim|{group_id}"),
            ],
            [
                Button(text="⚪️ Белый список", callback_data=f"g|kw|{group_id}|a"),
                Button(text="⚫️ Чёрный список", callback_data=f"g|kw|{group_id}|d"),
            ],
            [
                Button(text="👁 Предпросмотр", callback_data=f"g|prev|{group_id}"),
                Button(text="📨 Отправить сейчас", callback_data=f"g|send|{group_id}"),
            ],
            [
                Button(text="✏️ Имя", callback_data=f"g|ren|{group_id}"),
                Button(
                    text="⏸ Выключить" if group["enabled"] else "▶️ Включить",
                    callback_data=f"g|t|{group_id}",
                ),
                Button(text="🗑", callback_data=f"g|del|{group_id}"),
            ],
            [back("g", "⬅️ К списку")],
        ),
    )


# --------------------------------------------------------------------------- #
# create / rename / delete / toggle
# --------------------------------------------------------------------------- #


@router.callback_query(F.data == "g|new")
async def group_new(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(GroupName.creating)
    await show(
        call,
        "➕ <b>Новая группа</b>\n\nПришлите название. Можно начать с эмодзи — "
        "он попадёт в заголовок дайджеста.\n\nНапример: <code>🪙 Крипта</code>",
        kb([back("g", "⬅️ Отмена")]),
    )


@router.message(GroupName.creating)
async def group_create(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название не может быть пустым.")
        return
    emoji, name = _split_emoji(name)
    await state.clear()
    try:
        group_id = db.add_group(name[:64], emoji)
    except Exception:
        await message.answer("Группа с таким названием уже есть.")
        return
    await message.answer(
        f"✅ Группа <b>{emoji}{render.esc(name)}</b> создана.",
        reply_markup=kb([Button(text="Открыть", callback_data=f"g|v|{group_id}")]),
    )


def _split_emoji(text: str) -> tuple[str, str]:
    parts = text.split(maxsplit=1)
    if len(parts) == 2 and not parts[0].isalnum() and len(parts[0]) <= 4:
        return parts[0] + " ", parts[1].strip()
    return "", text


@router.callback_query(F.data.startswith("g|ren|"))
async def group_rename_prompt(call: CallbackQuery, state: FSMContext) -> None:
    group_id = int(call.data.rsplit("|", 1)[1])
    await state.set_state(GroupName.renaming)
    await state.update_data(group_id=group_id)
    await show(call, "✏️ Пришлите новое название.", kb([back(f"g|v|{group_id}", "⬅️ Отмена")]))


@router.message(GroupName.renaming)
async def group_rename(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    group_id = int(data.get("group_id", 0))
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название не может быть пустым.")
        return
    emoji, name = _split_emoji(name)
    await state.clear()
    db.update_group(group_id, name=name[:64], emoji=emoji)
    await message.answer(
        "✅ Переименовано.",
        reply_markup=kb([Button(text="Открыть", callback_data=f"g|v|{group_id}")]),
    )


@router.callback_query(F.data.startswith("g|t|"))
async def group_toggle(call: CallbackQuery) -> None:
    group_id = int(call.data.rsplit("|", 1)[1])
    group = db.group(group_id)
    if group is not None:
        db.update_group(group_id, enabled=0 if group["enabled"] else 1)
    call.data = f"g|v|{group_id}"
    await group_view(call)


@router.callback_query(F.data.startswith("g|del|"))
async def group_delete_confirm(call: CallbackQuery) -> None:
    group_id = int(call.data.rsplit("|", 1)[1])
    group = db.group(group_id)
    if group is None:
        await show(call, *_list_screen(0))
        return
    await show(
        call,
        f"🗑 Удалить группу <b>{render.esc(group['name'])}</b>?\n\n"
        "Сами каналы останутся — удалится только группа и её настройки.",
        kb(
            [
                Button(text="Да, удалить", callback_data=f"g|delc|{group_id}"),
                Button(text="Отмена", callback_data=f"g|v|{group_id}"),
            ]
        ),
    )


@router.callback_query(F.data.startswith("g|delc|"))
async def group_delete(call: CallbackQuery) -> None:
    db.delete_group(int(call.data.rsplit("|", 1)[1]))
    await show(call, *_list_screen(0), alert="Группа удалена")


# --------------------------------------------------------------------------- #
# channel assignment
# --------------------------------------------------------------------------- #


def _assign_screen(group_id: int, page: int) -> tuple[str, object]:
    group = db.group(group_id)
    channels = db.channels()
    attached = db.group_channel_ids(group_id)
    rows, page, total_pages = page_slice(channels, page)
    buttons = [
        [
            Button(
                text=f"{'☑️' if int(row['id']) in attached else '▫️'} @{cut(row['username'], 22)}"
                + (f" · {cut(row['title'], 16)}" if row["title"] else ""),
                callback_data=f"g|cht|{group_id}|{row['id']}|{page}",
            )
        ]
        for row in rows
    ]
    text = (
        f"📡 <b>Каналы группы «{render.esc(group['name']) if group else ''}»</b>\n\n"
        f"Отмечено: {len(attached)} из {len(channels)}. Нажатие переключает."
    )
    if not channels:
        text += "\n\n<i>Сначала добавьте каналы в разделе «Каналы».</i>"
    return text, kb(
        *buttons,
        pager(f"g|ch|{group_id}|", page, total_pages),
        [Button(text="➕ Добавить каналы", callback_data="ch|add"), back(f"g|v|{group_id}")],
    )


@router.callback_query(F.data.startswith("g|ch|"))
async def group_channels_screen(call: CallbackQuery) -> None:
    _, _, group_id, page = call.data.split("|")
    await show(call, *_assign_screen(int(group_id), int(page)))


@router.callback_query(F.data.startswith("g|cht|"))
async def group_channel_toggle(call: CallbackQuery) -> None:
    _, _, group_id, channel_id, page = call.data.split("|")
    db.toggle_group_channel(int(group_id), int(channel_id))
    await show(call, *_assign_screen(int(group_id), int(page)))


# --------------------------------------------------------------------------- #
# destination
# --------------------------------------------------------------------------- #


def _dest_screen(group_id: int, page: int, owner_id: int) -> tuple[str, object]:
    topics = db.topics()
    rows, page, total_pages = page_slice(topics, page)
    buttons = [
        [
            Button(
                text=f"💬 {cut(row['chat_title'] or row['chat_id'], 22)} › "
                f"{cut(row['title'] or 'General', 20)}",
                callback_data=f"g|dsts|{group_id}|{row['chat_id']}|{row['thread_id']}",
            )
        ]
        for row in rows
    ]
    text = (
        "📍 <b>Куда отправлять дайджест</b>\n\n"
        "Список тем бот пополняет сам: добавьте его в супергруппу, и любая "
        "активность в теме её зарегистрирует. Если тема тихая — напишите в ней "
        "<code>/bind</code>, бот запомнит её и сразу удалит своё сообщение."
    )
    if not topics:
        text += "\n\n<i>Пока ни одной темы не найдено.</i>"
    return text, kb(
        *buttons,
        pager(f"g|dst|{group_id}|", page, total_pages),
        [Button(text="✉️ Мне в личку", callback_data=f"g|dsts|{group_id}|{owner_id}|0")],
        [back(f"g|v|{group_id}")],
    )


@router.callback_query(F.data.startswith("g|dst|"))
async def group_dest_screen(call: CallbackQuery) -> None:
    _, _, group_id, page = call.data.split("|")
    await show(call, *_dest_screen(int(group_id), int(page), call.from_user.id))


@router.callback_query(F.data.startswith("g|dsts|"))
async def group_dest_set(call: CallbackQuery) -> None:
    _, _, group_id, chat_id, thread_id = call.data.split("|")
    group_id, chat_id, thread_id = int(group_id), int(chat_id), int(thread_id)

    if thread_id:
        row = db.topic(chat_id, thread_id)
        label = f"{row['chat_title']} › {row['title']}" if row else str(chat_id)
    else:
        label = "личные сообщения" if chat_id == call.from_user.id else str(chat_id)

    db.update_group(group_id, chat_id=chat_id, thread_id=thread_id or None, dest_name=label[:100])
    call.data = f"g|v|{group_id}"
    await group_view(call)


# --------------------------------------------------------------------------- #
# per-group overrides
# --------------------------------------------------------------------------- #


@router.callback_query(F.data.startswith("g|lim|"))
async def group_limit_prompt(call: CallbackQuery, state: FSMContext) -> None:
    group_id = int(call.data.rsplit("|", 1)[1])
    await state.set_state(GroupField.limit)
    await state.update_data(group_id=group_id)
    await show(
        call,
        f"🔢 <b>Постов на канал</b>\n\nСколько постов из каждого канала разбирать "
        f"подробно. Остальные уйдут в свёрнутую строку «Также писали про».\n\n"
        f"Пришлите число от 1 до 100, или <code>0</code> — чтобы использовать общую "
        f"настройку ({db.get('max_posts')}).",
        kb([back(f"g|v|{group_id}", "⬅️ Отмена")]),
    )


@router.message(GroupField.limit)
async def group_limit_set(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    group_id = int(data.get("group_id", 0))
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) > 100:
        await message.answer("Нужно число от 0 до 100.")
        return
    await state.clear()
    db.update_group(group_id, max_posts=int(raw) or None)
    await message.answer(
        "✅ Сохранено.", reply_markup=kb([Button(text="Открыть", callback_data=f"g|v|{group_id}")])
    )


@router.callback_query(F.data.startswith("g|kw|"))
async def group_keywords_prompt(call: CallbackQuery, state: FSMContext) -> None:
    _, _, group_id, mode = call.data.split("|")
    group = db.group(int(group_id))
    if group is None:
        await show(call, *_list_screen(0))
        return
    current = _kw(group["kw_allow" if mode == "a" else "kw_deny"])
    await state.set_state(GroupField.keywords)
    await state.update_data(group_id=int(group_id), mode=mode)

    title = "⚪️ Белый список" if mode == "a" else "⚫️ Чёрный список"
    rule = (
        "В дайджест попадут <b>только</b> посты, содержащие хотя бы одно из слов."
        if mode == "a"
        else "Посты с любым из этих слов будут пропущены."
    )
    await show(
        call,
        f"{title}\n\n{rule}\nФильтр применяется <b>до</b> обращения к ИИ, так что "
        f"он ещё и экономит токены.\n\n"
        f"Сейчас: {render.esc(', '.join(current)) if current else '<i>пусто</i>'}\n\n"
        f"Пришлите слова через запятую, или <code>-</code> чтобы очистить.",
        kb([back(f"g|v|{group_id}", "⬅️ Отмена")]),
    )


@router.message(GroupField.keywords)
async def group_keywords_set(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    group_id, mode = int(data.get("group_id", 0)), data.get("mode", "d")
    raw = (message.text or "").strip()
    words = [] if raw == "-" else [w.strip() for w in raw.split(",") if w.strip()][:50]
    await state.clear()
    field = "kw_allow" if mode == "a" else "kw_deny"
    db.update_group(group_id, **{field: json.dumps(words, ensure_ascii=False)})
    await message.answer(
        f"✅ Сохранено: {len(words)} слов." if words else "✅ Список очищен.",
        reply_markup=kb([Button(text="Открыть", callback_data=f"g|v|{group_id}")]),
    )


# --------------------------------------------------------------------------- #
# preview / send
# --------------------------------------------------------------------------- #


@router.callback_query(F.data.startswith("g|prev|"))
async def group_preview(
    call: CallbackQuery, bot: Bot, session: ClientSession, openrouter: OpenRouter
) -> None:
    group_id = int(call.data.rsplit("|", 1)[1])
    group = db.group(group_id)
    if group is None:
        await show(call, *_list_screen(0))
        return

    await show(call, "⏳ Собираю предпросмотр… Он придёт сюда и никуда больше.")
    result = await runner.run_group(bot, session, openrouter, group, preview_to=call.from_user.id)
    note = (
        f"👆 Предпросмотр «{render.esc(group['name'])}». Отправленным он не "
        f"считается — плановый дайджест придёт как обычно."
    )
    if result.errors:
        note += "\n\n" + "\n".join(f"⚠️ {render.esc(e)}" for e in result.errors[:5])
    await bot.send_message(
        call.from_user.id,
        note,
        reply_markup=kb([Button(text="⬅️ К группе", callback_data=f"g|v|{group_id}")]),
    )


@router.callback_query(F.data.startswith("g|send|"))
async def group_send(
    call: CallbackQuery, bot: Bot, session: ClientSession, openrouter: OpenRouter
) -> None:
    group_id = int(call.data.rsplit("|", 1)[1])
    group = db.group(group_id)
    if group is None or not group["chat_id"]:
        await call.answer("Сначала укажите, куда отправлять.", show_alert=True)
        return

    await show(call, "⏳ Собираю и отправляю…")
    result = await runner.run_group(bot, session, openrouter, group)
    lines = [
        "✅ <b>Отправлено</b>" if result.blocks else "ℹ️ <b>Нечего отправлять</b>",
        "",
        f"Постов: {result.total} · стоимость: ${result.cost:.4f}",
    ]
    if result.deduped:
        lines.append(f"Свёрнуто дублей: {result.deduped}")
    for err in result.errors[:5]:
        lines.append(f"⚠️ {render.esc(err)}")
    await show(call, "\n".join(lines), kb([back(f"g|v|{group_id}", "⬅️ К группе"), back()]))
