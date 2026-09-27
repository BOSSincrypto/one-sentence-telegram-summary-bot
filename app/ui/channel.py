"""Public channel screen: the ciscrypted-style digest destination.

The scheduled group digests live in topics of a supergroup; this screen owns
the separate daily 22:00 MSK post that goes to a standalone public channel
(e.g. ``@cryptovyzhimka``): destination, schedule, source set, ranking limits,
preview into the owner's DM and manual publishing.
"""

from __future__ import annotations

import re
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiohttp import ClientSession

from .. import cronsync, db, render, runner, tme
from ..config import Config
from ..digest import CHANNEL_SOURCE_CUSTOM
from ..llm import OpenRouter
from .common import Button, back, cut, kb, on_off, page_slice, pager, show, yes_no

router = Router(name="channel")

CH_NUMS: dict[str, tuple[str, int, int, str]] = {
    "channel_max_posts": (
        "Постов с канала",
        1,
        25,
        "Сколько постов каждого канала участвует в отборе.",
    ),
    "channel_total_max": (
        "Строк в выпуске",
        1,
        50,
        "Максимум строк в опубликованном дайджесте.",
    ),
    "channel_min_rank": (
        "Порог важности",
        1,
        5,
        "Посты с оценкой модели ниже этого не публикуются (5 — топ дня, 1 — мусор).",
    ),
}


class ChannelInput(StatesGroup):
    dest = State()
    time = State()
    number = State()


def validate_dest(raw: str) -> str:
    """Normalises a public-channel destination or raises ``ValueError``."""
    s = (raw or "").strip()
    if re.fullmatch(r"-100\d{5,}", s):
        return s
    return "@" + tme.normalize_username(s)


def _source_label() -> str:
    gid = int(db.get("channel_group_id") or 0)
    if gid == CHANNEL_SOURCE_CUSTOM:
        n = len(db.pub_source_channels(enabled_only=True))
        return f"набор канала ({n} акт.)"
    if gid:
        row = db.group(gid)
        if row is None:
            return "группа удалена — будут браться все каналы"
        n = len([r for r in db.group_channels(gid) if r["enabled"]])
        return f"группа «{row['name']}» ({n} акт.)"
    n = len(db.channels(enabled_only=True))
    return f"все каналы ({n} акт.)"


def _last_run_line() -> str:
    """One-line diagnosis of the latest channel run (or why there is none)."""
    for row in db.recent_runs(50):
        if int(row["group_id"]) != runner.CHANNEL_GROUP_ID:
            continue
        when = datetime.fromtimestamp(int(row["ts"]), render.tz()).strftime("%d.%m %H:%M")
        if not row["ok"]:
            return f"Последний выпуск: ❌ {when} — {render.esc((row['err'] or 'ошибка')[:160])}"
        return f"Последний выпуск: ✅ {when} — {row['posts']} постов, ${float(row['cost']):.4f}" + (
            f" — {render.esc((row['err'] or '')[:160])}" if row["err"] else ""
        )
    return "Последний выпуск: ещё не было"


def _status_text() -> str:
    dest = runner.channel_dest()
    upcoming = runner.next_channel_at()
    lines = [
        "📣 <b>Публичный канал</b>",
        "",
        f"Статус: <b>{on_off(db.get('channel_enabled'))}</b>",
        f"Канал: <b>{render.esc(dest) if dest else 'не задан'}</b>",
        f"🕙 Время: <b>{db.get('channel_time')}</b> · {db.get('tz')}",
        f"   ближайший выпуск: {upcoming:%d.%m %H:%M}",
        f"Источники: {_source_label()}",
        f"{_last_run_line()}",
        "",
        "<b>Отбор</b>",
        f"• Постов с канала: {db.get('channel_max_posts')}",
        f"• Строк в выпуске: {db.get('channel_total_max')}",
        f"• Порог важности: {db.get('channel_min_rank')}",
        "",
        (
            "<i>Формат — плоский, в стиле ciscrypted: «Имя: ярлык» со ссылкой, "
            "ранг 1–5 ставится той же моделью в том же запросе.</i>"
        ),
    ]
    if not dest:
        lines += ["", "⚠️ Укажите канал — без него выпуск не публикуется."]
    return "\n".join(lines)


def _status_kb():
    rows = [
        [
            Button(
                text=f"{yes_no(db.get('channel_enabled'))} Включён",
                callback_data="pub|toggle",
            ),
            Button(text="📍 Канал", callback_data="pub|dest"),
            Button(text="🕙 Время", callback_data="pub|time"),
        ],
        [Button(text="🗂 Источники", callback_data="pub|src")]
        + [
            Button(text=f"{label}: {db.get(key)}", callback_data=f"pub|n|{key}")
            for key, (label, _, _, _) in list(CH_NUMS.items())[:1]
        ],
        [
            Button(text=f"{CH_NUMS[k][0]}: {db.get(k)}", callback_data=f"pub|n|{k}")
            for k in list(CH_NUMS)[1:]
        ],
        [
            Button(text="👁 Предпросмотр", callback_data="pub|preview"),
            Button(text="🚀 Опубликовать", callback_data="pub|now"),
        ],
        [back()],
    ]
    return kb(*rows)


@router.callback_query(F.data == "pub")
async def channel_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await show(call, _status_text(), _status_kb())


@router.callback_query(F.data == "pub|toggle")
async def toggle_enabled(call: CallbackQuery) -> None:
    db.put("channel_enabled", not db.get("channel_enabled"))
    await show(call, _status_text(), _status_kb())


@router.callback_query(F.data == "pub|dest")
async def dest_prompt(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ChannelInput.dest)
    await show(
        call,
        "📍 <b>Канал для публикации</b>\n\nПришлите <code>@username</code> или "
        f"id вида <code>-100…</code>.\n\nСейчас: <b>{render.esc(runner.channel_dest() or '—')}</b>\n"
        "Бот должен быть администратором канала с правом публикации.",
        kb([back("pub", "⬅️ Отмена")]),
    )


@router.message(ChannelInput.dest)
async def dest_set(message: Message, state: FSMContext) -> None:
    try:
        dest = validate_dest(message.text or "")
    except (tme.ChannelError, ValueError):
        await message.answer("Нужно <code>@username</code> канала или id вида <code>-100…</code>.")
        return
    await state.clear()
    db.put("channel_dest", dest)
    await message.answer(
        f"✅ Канал: <b>{render.esc(dest)}</b>.",
        reply_markup=kb([Button(text="📣 К каналу", callback_data="pub")]),
    )


@router.callback_query(F.data == "pub|time")
async def time_prompt(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ChannelInput.time)
    await show(
        call,
        "🕙 <b>Время выпуска канала</b>\n\nПришлите время в формате <code>ЧЧ:ММ</code> "
        f"по поясу {db.get('tz')}.\n\nСейчас: <b>{db.get('channel_time')}</b>",
        kb([back("pub", "⬅️ Отмена")]),
    )


@router.message(ChannelInput.time)
async def time_set(
    message: Message, state: FSMContext, session: ClientSession, config: Config
) -> None:
    match = re.fullmatch(r"\s*(\d{1,2})\s*[:.\- ]\s*(\d{1,2})\s*", message.text or "")
    if not match:
        await message.answer("Формат — <code>ЧЧ:ММ</code>, например <code>22:00</code>.")
        return
    hour, minute = int(match[1]), int(match[2])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        await message.answer("Часы — от 0 до 23, минуты — от 0 до 59.")
        return
    await state.clear()
    db.put("channel_time", f"{hour:02d}:{minute:02d}")
    note = await _resync_channel(session, config)
    await message.answer(
        f"✅ Время выпуска: <b>{hour:02d}:{minute:02d}</b>.{note}",
        reply_markup=kb([Button(text="📣 К каналу", callback_data="pub")]),
    )


async def _resync_channel(session: ClientSession, config: Config) -> str:
    if not config.cronjob_key:
        return "\n\n⚠️ Ключ cron-job.org не задан — поправьте второй будильник вручную."
    hour, minute = runner.parse_time(db.get("channel_time") or "22:00")
    try:
        result = await cronsync.sync_channel(
            session, config.cronjob_key, config.tick_url, hour, minute
        )
        return f"\n\nБудильник синхронизирован: {result}."
    except cronsync.CronError as exc:
        return f"\n\n⚠️ Будильник не обновился: {render.esc(str(exc))}"


@router.callback_query(F.data.startswith("pub|n|"))
async def number_prompt(call: CallbackQuery, state: FSMContext) -> None:
    key = call.data.rsplit("|", 1)[1]
    if key not in CH_NUMS:
        await call.answer()
        return
    label, low, high, hint = CH_NUMS[key]
    await state.set_state(ChannelInput.number)
    await state.update_data(setting_key=key)
    await show(
        call,
        f"<b>{label}</b>\n\n{hint}\n\nСейчас: {db.get(key)}\nПришлите число от {low} до {high}.",
        kb([back("pub", "⬅️ Отмена")]),
    )


@router.message(ChannelInput.number)
async def number_set(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    key = str(data.get("setting_key", ""))
    if key not in CH_NUMS:
        await state.clear()
        return
    _, low, high, _ = CH_NUMS[key]
    raw = (message.text or "").strip()
    if not raw.lstrip("-").isdigit() or not low <= int(raw) <= high:
        await message.answer(f"Нужно целое число от {low} до {high}.")
        return
    await state.clear()
    db.put(key, int(raw))
    await message.answer(
        "✅ Сохранено.", reply_markup=kb([Button(text="📣 К каналу", callback_data="pub")])
    )


@router.callback_query(F.data == "pub|src")
async def source_menu(call: CallbackQuery) -> None:
    current = int(db.get("channel_group_id") or 0)
    custom_n = len(db.pub_source_ids())
    rows: list[list[Button]] = [
        [
            Button(
                text=f"{'🔘' if current == 0 else '⚪️'} Все каналы",
                callback_data="pub|src|0",
            )
        ],
        [
            Button(
                text=f"{'🔘' if current == CHANNEL_SOURCE_CUSTOM else '⚪️'} "
                f"📣 Набор канала ({custom_n})",
                callback_data="pub|src|custom",
            )
        ],
    ]
    if current == CHANNEL_SOURCE_CUSTOM:
        rows.append([Button(text="📝 Настроить набор", callback_data="pub|set|0")])
    for group in db.groups():
        n = len(db.group_channels(int(group["id"])))
        mark = "🔘" if current == int(group["id"]) else "⚪️"
        rows.append(
            [Button(text=f"{mark} {group['name']} ({n})", callback_data=f"pub|src|{group['id']}")]
        )
    rows.append([back("pub")])
    await show(call, "🗂 <b>Источники выпуска</b>\n\nЧьи каналы собирать в канал:", kb(*rows))


@router.callback_query(F.data.startswith("pub|src|"))
async def source_set(call: CallbackQuery) -> None:
    raw = call.data.rsplit("|", 1)[1]
    if raw == "0":
        db.put("channel_group_id", 0)
    elif raw == "custom":
        db.put("channel_group_id", CHANNEL_SOURCE_CUSTOM)
        if not db.pub_source_ids():
            # Start from today's enabled set so the owner only unticks extras.
            db.fill_pub_source([int(r["id"]) for r in db.channels(enabled_only=True)])
    else:
        row = db.group(int(raw)) if raw.isdigit() else None
        if row is None:
            await call.answer("Группа не найдена.", show_alert=True)
            return
        db.put("channel_group_id", int(row["id"]))
    await show(call, _status_text(), _status_kb())


def _custom_screen(page: int) -> tuple[str, object]:
    channels = db.channels()
    attached = db.pub_source_ids()
    rows, page, total_pages = page_slice(channels, page)
    buttons = [
        [
            Button(
                text=f"{'☑️' if int(row['id']) in attached else '▫️'} @{cut(row['username'], 22)}"
                + (f" · {cut(row['title'], 16)}" if row["title"] else ""),
                callback_data=f"pub|sett|{row['id']}|{page}",
            )
        ]
        for row in rows
    ]
    text = (
        "📣 <b>Набор канала</b>\n\n"
        f"Отмечено: {len(attached)} из {len(channels)}. Нажатие переключает. "
        "Групповые дайджесты этот набор не затрагивает."
    )
    if not channels:
        text += "\n\n<i>Сначала добавьте каналы в разделе «Каналы».</i>"
    return text, kb(
        *buttons,
        pager("pub|set|", page, total_pages),
        [back("pub|src")],
    )


@router.callback_query(F.data.startswith("pub|set|"))
async def custom_screen(call: CallbackQuery) -> None:
    raw = call.data.rsplit("|", 1)[1]
    await show(call, *_custom_screen(int(raw) if raw.isdigit() else 0))


@router.callback_query(F.data.startswith("pub|sett|"))
async def custom_toggle(call: CallbackQuery) -> None:
    try:
        _, _, channel_id, page = call.data.split("|")
        db.toggle_pub_source(int(channel_id))
    except (ValueError, IndexError):
        await call.answer()
        return
    await show(call, *_custom_screen(int(page) if page.isdigit() else 0))


def _result_lines(result) -> str:
    lines = [
        f"• Постов: {result.total}",
        f"• Стоимость: ${result.cost:.4f}",
    ]
    if result.models:
        lines.append(f"• Модель: {render.esc(', '.join(result.models))}")
    for err in result.errors[:3]:
        lines.append(f"• ⚠️ {render.esc(err)}")
    return "\n".join(lines)


@router.callback_query(F.data == "pub|preview")
async def preview(
    call: CallbackQuery, bot: Bot, session: ClientSession, openrouter: OpenRouter
) -> None:
    user_id = call.from_user.id if call.from_user else None
    if user_id is None:
        await call.answer("Не вижу отправителя.", show_alert=True)
        return
    await show(call, "⏳ Собираю предпросмотр канала…")
    try:
        result = await runner.run_channel(bot, session, openrouter, preview_to=user_id)
    except Exception as exc:  # never leave the button hanging
        await show(call, f"❌ Ошибка: {render.esc(repr(exc))}", _status_kb())
        return
    if result is None:
        await show(call, "❌ Канал не задан — укажите его кнопкой «📍 Канал».", _status_kb())
        return
    await show(
        call,
        "✅ <b>Предпросмотр отправлен вам в личку</b> (день не отмечен, повтор бесплатен).\n\n"
        + _result_lines(result),
        _status_kb(),
    )


@router.callback_query(F.data == "pub|now")
async def publish_now(
    call: CallbackQuery, bot: Bot, session: ClientSession, openrouter: OpenRouter
) -> None:
    if not runner.channel_dest():
        await call.answer("Сначала укажите канал.", show_alert=True)
        return
    await show(call, "⏳ Публикую выпуск в канал…")
    try:
        result = await runner.run_channel(bot, session, openrouter, force=True)
    except Exception as exc:  # never leave the button hanging
        await show(call, f"❌ Ошибка: {render.esc(repr(exc))}", _status_kb())
        return
    if result is None:
        await show(call, "❌ Канал не задан.", _status_kb())
        return
    await show(call, "✅ <b>Выпуск опубликован</b>.\n\n" + _result_lines(result), _status_kb())
