"""Global settings: schedule, selection rules, rendering, wake-up alarm."""

from __future__ import annotations

import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiohttp import ClientSession

from .. import cronsync, db, render, runner
from ..config import Config
from .common import Button, back, kb, on_off, show

router = Router(name="settings")

BOOLS: dict[str, str] = {
    "skip_media_only": "Пропускать посты без текста",
    "skip_ads": "Пропускать рекламу",
    "dedupe": "Сворачивать дубли между каналами",
    "no_repeat": "Не повторять уже отправленное",
    "overflow": "Блок «Также писали про»",
    "show_time": "Показывать время поста",
    "show_views": "Показывать просмотры",
    "notify_errors": "Писать мне об ошибках",
}

NUMS: dict[str, tuple[str, int, int, str]] = {
    "window_hours": ("Окно сбора, часов", 1, 168, "За какой период собирать посты."),
    "max_posts": ("Постов на канал", 1, 100, "Сколько постов разбирать подробно."),
    "overflow_max": (
        "Максимум в «Также писали»",
        0,
        100,
        "Сколько постов уходит во вторую строку.",
    ),
    "dedupe_dist": (
        "Порог схожести дублей",
        0,
        16,
        (
            "Насколько тексты должны совпадать, чтобы считаться одной новостью. "
            "Меньше — строже; 3 ловит репосты и копипасту."
        ),
    ),
    "collapse_at": ("Сворачивать канал от, постов", 1, 100, "Длинные каналы прячутся под цитату."),
    "short_verbatim": (
        "Короткие посты без ИИ, символов",
        0,
        400,
        "Пост короче этого показывается как есть — модель не вызывается.",
    ),
    "concurrency": ("Параллельных загрузок", 1, 8, "Сколько каналов читать одновременно."),
    "max_pages": ("Страниц на канал", 1, 25, "Одна страница t.me — это 20 постов."),
}


class SettingInput(StatesGroup):
    number = State()
    time = State()
    timezone = State()


def _menu_text() -> str:
    upcoming = runner.next_run_at()
    lines = [
        "⚙️ <b>Настройки</b>",
        "",
        f"🕘 Время дайджеста: <b>{db.get('digest_time')}</b> · {db.get('tz')}",
        f"   ближайший запуск: {upcoming:%d.%m %H:%M}",
        f"🪟 Окно сбора: {db.get('window_hours')} ч",
        "",
        "<b>Отбор</b>",
        f"• Постов на канал: {db.get('max_posts')}",
        f"• «Также писали про»: {on_off(db.get('overflow'))} (до {db.get('overflow_max')})",
        f"• Без текста: {'пропускать' if db.get('skip_media_only') else 'оставлять'}",
        f"• Реклама: {'пропускать' if db.get('skip_ads') else 'оставлять'}",
        f"• Дубли: {on_off(db.get('dedupe'))} (порог {db.get('dedupe_dist')})",
        f"• Повторы: {'не слать' if db.get('no_repeat') else 'разрешены'}",
        "",
        "<b>Оформление</b>",
        f"• Сворачивать канал от: {db.get('collapse_at')} постов",
        f"• Время поста: {on_off(db.get('show_time'))} · просмотры: {on_off(db.get('show_views'))}",
        "",
        "<b>Служебное</b>",
        f"• Уведомления об ошибках: {on_off(db.get('notify_errors'))}",
        f"• Параллельных загрузок: {db.get('concurrency')} · страниц на канал: {db.get('max_pages')}",
    ]
    return "\n".join(lines)


def _menu_kb():
    bool_rows = []
    keys = list(BOOLS)
    for i in range(0, len(keys), 2):
        bool_rows.append(
            [
                Button(
                    text=f"{'✅' if db.get(k) else '⬜️'} {BOOLS[k]}",
                    callback_data=f"s|b|{k}",
                )
                for k in keys[i : i + 2]
            ]
        )
    num_rows = []
    num_keys = list(NUMS)
    for i in range(0, len(num_keys), 2):
        num_rows.append(
            [
                Button(text=f"{NUMS[k][0]}: {db.get(k)}", callback_data=f"s|n|{k}")
                for k in num_keys[i : i + 2]
            ]
        )
    return kb(
        [
            Button(text="🕘 Время", callback_data="s|time"),
            Button(text="🌍 Часовой пояс", callback_data="s|tz"),
            Button(text="⏰ Будильник", callback_data="s|cron"),
        ],
        *bool_rows,
        *num_rows,
        [back()],
    )


@router.callback_query(F.data == "s")
async def settings_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await show(call, _menu_text(), _menu_kb())


@router.callback_query(F.data.startswith("s|b|"))
async def toggle_bool(call: CallbackQuery) -> None:
    key = call.data.rsplit("|", 1)[1]
    if key in BOOLS:
        db.put(key, not db.get(key))
    await show(call, _menu_text(), _menu_kb())


@router.callback_query(F.data.startswith("s|n|"))
async def number_prompt(call: CallbackQuery, state: FSMContext) -> None:
    key = call.data.rsplit("|", 1)[1]
    if key not in NUMS:
        await call.answer()
        return
    label, low, high, hint = NUMS[key]
    await state.set_state(SettingInput.number)
    await state.update_data(setting_key=key)
    await show(
        call,
        f"<b>{label}</b>\n\n{hint}\n\nСейчас: {db.get(key)}\nПришлите число от {low} до {high}.",
        kb([back("s", "⬅️ Отмена")]),
    )


@router.message(SettingInput.number)
async def number_set(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    key = str(data.get("setting_key", ""))
    if key not in NUMS:
        await state.clear()
        return
    _, low, high, _ = NUMS[key]
    raw = (message.text or "").strip()
    if not raw.lstrip("-").isdigit() or not low <= int(raw) <= high:
        await message.answer(f"Нужно целое число от {low} до {high}.")
        return
    await state.clear()
    db.put(key, int(raw))
    await message.answer(
        "✅ Сохранено.", reply_markup=kb([Button(text="⚙️ К настройкам", callback_data="s")])
    )


# --------------------------------------------------------------------------- #
# schedule
# --------------------------------------------------------------------------- #


@router.callback_query(F.data == "s|time")
async def time_prompt(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingInput.time)
    await show(
        call,
        "🕘 <b>Время дайджеста</b>\n\nПришлите время в формате <code>ЧЧ:ММ</code> "
        f"по поясу {db.get('tz')}.\n\nНапример: <code>09:00</code>",
        kb([back("s", "⬅️ Отмена")]),
    )


@router.message(SettingInput.time)
async def time_set(
    message: Message, state: FSMContext, session: ClientSession, config: Config
) -> None:
    match = re.fullmatch(r"\s*(\d{1,2})\s*[:.\- ]\s*(\d{1,2})\s*", message.text or "")
    if not match:
        await message.answer("Формат — <code>ЧЧ:ММ</code>, например <code>09:00</code>.")
        return
    hour, minute = int(match[1]), int(match[2])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        await message.answer("Часы — от 0 до 23, минуты — от 0 до 59.")
        return
    await state.clear()
    db.put("digest_time", f"{hour:02d}:{minute:02d}")
    note = await _resync(session, config)
    await message.answer(
        f"✅ Время дайджеста: <b>{hour:02d}:{minute:02d}</b>.{note}",
        reply_markup=kb([Button(text="⚙️ К настройкам", callback_data="s")]),
    )


@router.callback_query(F.data == "s|tz")
async def tz_prompt(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingInput.timezone)
    await show(
        call,
        "🌍 <b>Часовой пояс</b>\n\nПришлите имя пояса в формате IANA.\n\n"
        "Например: <code>Europe/Moscow</code>, <code>Europe/Kyiv</code>, "
        "<code>Asia/Almaty</code>, <code>UTC</code>",
        kb([back("s", "⬅️ Отмена")]),
    )


@router.message(SettingInput.timezone)
async def tz_set(
    message: Message, state: FSMContext, session: ClientSession, config: Config
) -> None:
    raw = (message.text or "").strip()
    try:
        ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError):
        await message.answer("Такого пояса нет. Нужен формат вида <code>Europe/Moscow</code>.")
        return
    await state.clear()
    db.put("tz", raw)
    note = await _resync(session, config)
    await message.answer(
        f"✅ Часовой пояс: <b>{render.esc(raw)}</b>.{note}",
        reply_markup=kb([Button(text="⚙️ К настройкам", callback_data="s")]),
    )


async def _resync(session: ClientSession, config: Config) -> str:
    if not config.cronjob_key:
        return "\n\n⚠️ Не забудьте поправить расписание будильника вручную — см. «⏰ Будильник»."
    hour, minute = runner.parse_time(db.get("digest_time"))
    try:
        result = await cronsync.sync(session, config.cronjob_key, config.tick_url, hour, minute)
        return f"\n\nБудильник синхронизирован: {result}."
    except cronsync.CronError as exc:
        return f"\n\n⚠️ Будильник не обновился: {render.esc(str(exc))}"


# --------------------------------------------------------------------------- #
# wake-up alarm
# --------------------------------------------------------------------------- #


@router.callback_query(F.data == "s|cron")
async def cron_screen(call: CallbackQuery, session: ClientSession, config: Config) -> None:
    hour, minute = runner.parse_time(db.get("digest_time"))
    schedule = cronsync.describe(hour, minute, str(db.get("tz")))
    managed = bool(config.cronjob_key)

    lines = [
        "⏰ <b>Будильник</b>",
        "",
        (
            "Машина на Fly спит, когда ничего не делает — так она стоит копейки. "
            "Разбудить её по расписанию Fly не умеет, поэтому наружу торчит адрес "
            "<code>/tick</code>: запрос на него поднимает машину, она проверяет, "
            "не пора ли слать дайджест, и снова засыпает."
        ),
        "",
        f"<b>Адрес:</b>\n<code>{render.esc(config.tick_url)}</code>",
        f"<b>Расписание:</b>\n<code>{render.esc(schedule)}</code>",
        "",
    ]
    if managed:
        lines.append("✅ Ключ cron-job.org задан — бот управляет расписанием сам.")
        try:
            status = await cronsync.status(session, config.cronjob_key)
            if status:
                lines.append(f"   {render.esc(status)}")
        except cronsync.CronError as exc:
            lines.append(f"   ⚠️ {render.esc(str(exc))}")
    else:
        lines.append(
            "ℹ️ Переменная <code>CRONJOB_API_KEY</code> не задана. Создайте задачу "
            "на cron-job.org вручную с адресом и расписанием выше — либо добавьте "
            "ключ, и бот будет держать расписание в актуальном виде сам."
        )
    lines.append("\n<i>Адрес содержит секрет — не публикуйте его.</i>")

    rows = []
    if managed:
        rows.append([Button(text="🔄 Синхронизировать", callback_data="s|cronsync")])
    rows.append([back("s")])
    await show(call, "\n".join(lines), kb(*rows))


@router.callback_query(F.data == "s|cronsync")
async def cron_sync_now(call: CallbackQuery, session: ClientSession, config: Config) -> None:
    hour, minute = runner.parse_time(db.get("digest_time"))
    try:
        result = await cronsync.sync(session, config.cronjob_key, config.tick_url, hour, minute)
    except cronsync.CronError as exc:
        await call.answer(str(exc)[:180], show_alert=True)
        return
    await call.answer(f"Готово: {result}", show_alert=True)
