"""Cost statistics, run history, config export/import."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from .. import db, render
from .common import Button, back, cut, kb, show

router = Router(name="stats")

EXPORT_KEYS = tuple(db.DEFAULTS)


class ImportConfig(StatesGroup):
    waiting = State()


def _stats_text() -> str:
    since = (db.local_date() - timedelta(days=30)).isoformat()
    rows = db.usage_since(since)
    month = db.month_cost(db.local_date().strftime("%Y-%m"))
    budget = float(db.get("budget_usd") or 0)

    lines = ["📊 <b>Статистика</b>", "", "<b>ИИ за 30 дней</b>"]
    if rows:
        for row in rows:
            lines.append(
                f"• <code>{render.esc(cut(row['model'], 38))}</code>\n"
                f"  {row['calls']} вызовов · {row['pt'] / 1000:.0f}K вх / "
                f"{row['ct'] / 1000:.0f}K исх · ${row['cost']:.4f}"
            )
    else:
        lines.append("<i>обращений не было</i>")

    lines += [
        "",
        f"💰 В этом месяце: <b>${month:.4f}</b>"
        + (f" из ${budget:.2f}" if budget else " (без лимита)"),
        "",
        "<b>Последние запуски</b>",
    ]
    runs = db.recent_runs(8)
    if runs:
        zone = render.tz()
        for run in runs:
            when = datetime.fromtimestamp(run["ts"], zone).strftime("%d.%m %H:%M")
            group = db.group(int(run["group_id"]))
            name = group["name"] if group else "—"
            mark = "✅" if run["ok"] else "⚠️"
            lines.append(
                f"{mark} {when} · {render.esc(cut(name, 20))} · {run['posts']} п. · "
                f"${run['cost']:.4f} · {run['ms'] / 1000:.1f} с"
            )
            if run["err"]:
                lines.append(f"    <i>{render.esc(cut(run['err'], 90))}</i>")
    else:
        lines.append("<i>ещё не запускался</i>")

    counts = (
        db.db()
        .execute("SELECT (SELECT COUNT(*) FROM summary) s, (SELECT COUNT(*) FROM sent) t")
        .fetchone()
    )
    lines += [
        "",
        f"<i>Кэш саммари: {counts['s']} записей · отметок об отправке: {counts['t']}</i>",
    ]
    return "\n".join(lines)


@router.callback_query(F.data == "st")
async def stats_screen(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await show(
        call,
        _stats_text(),
        kb(
            [
                Button(text="⬆️ Экспорт конфига", callback_data="st|exp"),
                Button(text="⬇️ Импорт конфига", callback_data="st|imp"),
            ],
            [back()],
        ),
    )


def export_payload() -> dict:
    settings = {k: db.get(k) for k in EXPORT_KEYS if k not in {"openrouter_key", "cron_job_id"}}
    return {
        "version": 1,
        "exported_at": datetime.now(render.tz()).isoformat(timespec="seconds"),
        "settings": settings,
        "channels": [
            {"username": row["username"], "title": row["title"], "enabled": bool(row["enabled"])}
            for row in db.channels()
        ],
        "groups": [
            {
                "name": row["name"],
                "emoji": row["emoji"],
                "enabled": bool(row["enabled"]),
                "chat_id": row["chat_id"],
                "thread_id": row["thread_id"],
                "dest_name": row["dest_name"],
                "model": row["model"],
                "max_posts": row["max_posts"],
                "kw_allow": json.loads(row["kw_allow"] or "[]"),
                "kw_deny": json.loads(row["kw_deny"] or "[]"),
                "channels": [c["username"] for c in db.group_channels(int(row["id"]))],
            }
            for row in db.groups()
        ],
    }


@router.callback_query(F.data == "st|exp")
async def export_config(call: CallbackQuery, bot: Bot) -> None:
    payload = json.dumps(export_payload(), ensure_ascii=False, indent=2).encode()
    await call.answer()
    await bot.send_document(
        call.from_user.id,
        BufferedInputFile(payload, filename="digest-bot-config.json"),
        caption=(
            "⬆️ Конфигурация: каналы, группы и настройки.\n"
            "<b>Ключ OpenRouter в файл не попадает.</b> Храните файл — он "
            "восстановит всё, если том на Fly будет потерян."
        ),
    )


@router.callback_query(F.data == "st|imp")
async def import_prompt(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ImportConfig.waiting)
    await show(
        call,
        "⬇️ <b>Импорт конфигурации</b>\n\nПришлите файл, полученный при экспорте.\n\n"
        "Каналы и группы <b>добавятся к текущим</b>: совпадающие по имени "
        "обновятся, остальные будут созданы. Ничего не удаляется.",
        kb([back("st", "⬅️ Отмена")]),
    )


@router.message(ImportConfig.waiting, F.document)
async def import_config(message: Message, state: FSMContext, bot: Bot) -> None:
    document = message.document
    if document.file_size and document.file_size > 1_000_000:
        await message.answer("Файл слишком большой — ожидается конфиг до 1 МБ.")
        return

    buffer = await bot.download(document)
    await state.clear()
    try:
        payload = json.loads(buffer.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        await message.answer("Не получилось прочитать файл — нужен JSON от экспорта.")
        return
    if not isinstance(payload, dict):
        await message.answer("Формат не распознан.")
        return

    settings = payload.get("settings")
    applied = 0
    if isinstance(settings, dict):
        for key, value in settings.items():
            if key in db.DEFAULTS and key not in {"openrouter_key", "cron_job_id"}:
                db.put(key, value)
                applied += 1

    added_channels = 0
    for entry in payload.get("channels") or []:
        if isinstance(entry, dict) and entry.get("username"):
            db.add_channel(str(entry["username"]).lower(), str(entry.get("title") or ""))
            added_channels += 1

    by_name = {row["name"]: int(row["id"]) for row in db.groups()}
    ids = {row["username"]: int(row["id"]) for row in db.channels()}
    added_groups = 0
    for entry in payload.get("groups") or []:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        name = str(entry["name"])[:64]
        group_id = by_name.get(name) or db.add_group(name, str(entry.get("emoji") or ""))
        db.update_group(
            group_id,
            emoji=str(entry.get("emoji") or ""),
            enabled=int(bool(entry.get("enabled", True))),
            chat_id=entry.get("chat_id"),
            thread_id=entry.get("thread_id"),
            dest_name=str(entry.get("dest_name") or "")[:100],
            model=str(entry.get("model") or ""),
            max_posts=entry.get("max_posts"),
            kw_allow=json.dumps(entry.get("kw_allow") or [], ensure_ascii=False),
            kw_deny=json.dumps(entry.get("kw_deny") or [], ensure_ascii=False),
        )
        attached = db.group_channel_ids(group_id)
        for username in entry.get("channels") or []:
            channel_id = ids.get(str(username).lower())
            if channel_id and channel_id not in attached:
                db.toggle_group_channel(group_id, channel_id)
        added_groups += 1

    await message.answer(
        f"✅ Импортировано\n\n"
        f"Настроек: {applied}\nКаналов: {added_channels}\nГрупп: {added_groups}\n\n"
        f"<i>Ключ OpenRouter импорт не трогает.</i>",
        reply_markup=kb([Button(text="⬅️ В меню", callback_data="m")]),
    )


@router.message(ImportConfig.waiting)
async def import_wrong_type(message: Message) -> None:
    await message.answer("Пришлите именно файл (документ) с конфигурацией.")
