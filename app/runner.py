"""Orchestration: decide what is due, build it, deliver it, record it."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiohttp import ClientSession

from . import db, render
from . import digest as digest_mod
from .llm import OpenRouter

log = logging.getLogger("runner")

LAST_DAY_KEY = "_last_digest_day"
_lock = asyncio.Lock()


def parse_time(raw: str) -> tuple[int, int]:
    try:
        hh, _, mm = str(raw).partition(":")
        h, m = int(hh), int(mm or 0)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except ValueError:
        pass
    return 9, 0


def next_run_at(now: datetime | None = None) -> datetime:
    """When the next digest is expected, in the configured timezone."""
    zone = render.tz()
    local = (now or datetime.now(UTC)).astimezone(zone)
    hour, minute = parse_time(db.get("digest_time"))
    scheduled = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if local < scheduled:
        return scheduled
    if db.get(LAST_DAY_KEY) != local.date().isoformat():
        return local  # overdue: the next tick will pick it up
    return scheduled + timedelta(days=1)


def is_due(now: datetime | None = None) -> bool:
    zone = render.tz()
    local = (now or datetime.now(UTC)).astimezone(zone)
    hour, minute = parse_time(db.get("digest_time"))
    scheduled = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if local < scheduled:
        return False
    return db.get(LAST_DAY_KEY) != local.date().isoformat()


async def send_messages(
    bot: Bot,
    chat_id: int,
    thread_id: int | None,
    messages: list[str],
) -> None:
    for text in messages:
        for attempt in range(3):
            try:
                await bot.send_message(chat_id, text, message_thread_id=thread_id)
                break
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
            except TelegramAPIError:
                if attempt == 2:
                    raise
                await asyncio.sleep(1.5 * (attempt + 1))
        # Telegram allows ~20 messages/minute to the same group chat.
        if len(messages) > 1:
            await asyncio.sleep(0.4)


async def run_group(
    bot: Bot,
    session: ClientSession,
    client: OpenRouter,
    group: sqlite3.Row,
    *,
    preview_to: int | None = None,
) -> digest_mod.Digest:
    """Builds one group's digest and delivers it.

    ``preview_to`` sends the result to that chat instead of the group's
    destination, ignores the already-sent ledger and does not record delivery —
    so a preview can be run as many times as you like without burning the
    scheduled digest.
    """
    started = time.perf_counter()
    preview = preview_to is not None
    result = await digest_mod.build(session, client, group, respect_sent=not preview)
    messages = render.render(result, verbose=preview)

    error = ""
    try:
        if preview:
            await send_messages(bot, preview_to, None, messages)
        elif result.blocks:
            await send_messages(bot, int(group["chat_id"]), group["thread_id"], messages)
            db.mark_sent(int(group["id"]), result.pairs)
    except TelegramAPIError as exc:
        error = f"Telegram: {exc.__class__.__name__}: {exc}"
        result.errors.append(error)

    if not preview:
        db.log_run(
            int(group["id"]),
            ok=not error,
            posts=result.total,
            cost=result.cost,
            ms=int((time.perf_counter() - started) * 1000),
            err=error or "; ".join(result.errors)[:300],
        )
    return result


async def run_all(bot: Bot, session: ClientSession, client: OpenRouter) -> list[digest_mod.Digest]:
    """Runs every enabled, bound group. Guarded so two ticks cannot overlap."""
    if _lock.locked():
        log.info("run_all skipped: another run is in progress")
        return []

    async with _lock:
        groups = db.groups(enabled_only=True)
        if not groups:
            return []

        results: list[digest_mod.Digest] = []
        for group in groups:
            try:
                results.append(await run_group(bot, session, client, group))
            except Exception as exc:  # pragma: no cover - never kill the loop
                log.exception("group %s failed", group["id"])
                db.log_run(int(group["id"]), ok=False, posts=0, cost=0.0, ms=0, err=repr(exc))

        zone = render.tz()
        db.put(LAST_DAY_KEY, datetime.now(zone).date().isoformat())
        db.prune()
        return results


async def notify_problems(bot: Bot, owner_ids, results: list[digest_mod.Digest]) -> None:
    if not db.get("notify_errors"):
        return
    problems: list[str] = []
    for result in results:
        for err in result.errors:
            problems.append(f"• <b>{render.esc(result.group_name)}</b>: {render.esc(err)}")
    if not problems:
        return
    text = "⚠️ <b>Проблемы при сборе дайджеста</b>\n\n" + "\n".join(problems[:20])
    for owner in owner_ids:
        try:
            await bot.send_message(owner, text[: render.SAFE])
        except TelegramAPIError:
            log.warning("could not notify owner %s", owner)
