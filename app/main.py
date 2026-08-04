"""Entry point: webhook server, /tick endpoint, graceful shutdown.

On Fly the Machine is stopped whenever it is idle. That shapes the design:

* updates arrive by **webhook** — an inbound request is what wakes the Machine,
  so long polling (which needs a permanently running process) is dev-only;
* the digest is triggered by **/tick**, called from outside on a schedule,
  because Fly does not wake Machines on a timer;
* nothing important lives in process memory — see :mod:`app.fsm`.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import ClientSession, ClientTimeout, TCPConnector, web

from . import config as config_mod
from . import cronsync, db, runner, ui
from .fsm import SqliteStorage
from .llm import OpenRouter

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("main")

TICK_SYNC_TIMEOUT = 25  # keep the caller's connection open this long, then detach

# Typed keys, and a mutable holder created before startup: aiohttp forbids
# writing new keys into a running Application, and the in-flight digest task
# has to be recorded while the app is serving.
BOT = web.AppKey("bot", Bot)
SESSION = web.AppKey("session", ClientSession)
OPENROUTER = web.AppKey("openrouter", OpenRouter)
CONFIG = web.AppKey("config", object)
RUNTIME = web.AppKey("runtime", dict)
DP = web.AppKey("dp", Dispatcher)

COMMANDS = [
    BotCommand(command="menu", description="Открыть меню"),
    BotCommand(command="bind", description="Привязать текущую тему (в группе)"),
    BotCommand(command="id", description="Показать id чата и темы"),
]


async def _run_due(app: web.Application) -> None:
    bot, session, client = app[BOT], app[SESSION], app[OPENROUTER]
    results = await runner.run_all(bot, session, client)
    if results:
        await runner.notify_problems(bot, app[CONFIG].owner_ids, results)


def _log_task_result(task: asyncio.Task) -> None:
    """Makes sure a failure is never swallowed when nobody is awaiting."""
    if not task.cancelled() and task.exception() is not None:
        log.exception("digest run failed", exc_info=task.exception())


async def handle_tick(request: web.Request) -> web.Response:
    app = request.app
    cfg = app[CONFIG]
    if not hmac.compare_digest(request.query.get("key", ""), cfg.tick_key):
        return web.Response(status=403, text="forbidden")

    force = request.query.get("force") == "1"
    if not force and not runner.is_due():
        return web.json_response({"status": "idle", "next": runner.next_run_at().isoformat()})

    runtime = app[RUNTIME]
    task = runtime.get("tick_task")
    if task is not None and not task.done():
        return web.json_response({"status": "running"})

    task = asyncio.create_task(_run_due(app))
    task.add_done_callback(_log_task_result)
    runtime["tick_task"] = task

    # Answer only once the work is done, when it is quick: an open connection
    # is also what keeps fly-proxy from stopping the Machine mid-digest.
    with suppress(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=TICK_SYNC_TIMEOUT)
        return web.json_response({"status": "done"})
    return web.json_response({"status": "accepted"}, status=202)


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "next": runner.next_run_at().isoformat()})


async def _on_startup(app: web.Application) -> None:
    bot, cfg, dp = app[BOT], app[CONFIG], app[DP]
    session = ClientSession(
        connector=TCPConnector(limit=16, ttl_dns_cache=600),
        timeout=ClientTimeout(total=180),
    )
    client = OpenRouter(session)
    app[SESSION], app[OPENROUTER] = session, client
    dp.workflow_data.update(session=session, openrouter=client)

    await bot.set_my_commands(COMMANDS)
    await bot.set_webhook(
        cfg.webhook_url,
        secret_token=cfg.webhook_secret,
        allowed_updates=["message", "callback_query", "my_chat_member"],
        drop_pending_updates=False,
    )
    log.info("webhook set to %s", cfg.webhook_url)

    if cfg.cronjob_key and not int(db.get("cron_job_id") or 0):
        hour, minute = runner.parse_time(db.get("digest_time"))
        try:
            result = await cronsync.sync(app[SESSION], cfg.cronjob_key, cfg.tick_url, hour, minute)
            log.info("cron-job.org: %s", result)
        except cronsync.CronError as exc:
            log.warning("cron-job.org sync failed: %s", exc)


async def _on_cleanup(app: web.Application) -> None:
    task = app[RUNTIME].get("tick_task")
    if task is not None and not task.done():
        log.info("waiting for the in-flight digest to finish")
        with suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=20)
    session = app.get(SESSION)
    if session is not None:
        await session.close()
    await app[BOT].session.close()
    db.close()


def build_app(cfg: config_mod.Config) -> web.Application:
    bot = Bot(
        cfg.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )

    dp = Dispatcher(storage=SqliteStorage())
    dp.include_router(ui.build_router(cfg.owner_ids))
    dp.workflow_data.update(config=cfg, owner_ids=cfg.owner_ids)

    app = web.Application()
    app[BOT], app[CONFIG], app[DP] = bot, cfg, dp
    app[RUNTIME] = {}
    app.router.add_route("*", "/tick", handle_tick)
    app.router.add_get("/health", handle_health)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)

    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=cfg.webhook_secret).register(
        app, path=cfg.webhook_path
    )
    setup_application(app, dp, bot=bot)
    return app


async def _dev_scheduler(bot: Bot, session: ClientSession, client: OpenRouter) -> None:
    """Polling mode only: no external alarm exists locally."""
    while True:
        await asyncio.sleep(60)
        if runner.is_due():
            with suppress(Exception):
                await runner.run_all(bot, session, client)


async def _run_polling(cfg: config_mod.Config) -> None:
    bot = Bot(
        cfg.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    session = ClientSession(timeout=ClientTimeout(total=180))
    client = OpenRouter(session)
    dp = Dispatcher(storage=SqliteStorage())
    dp.include_router(ui.build_router(cfg.owner_ids))
    dp.workflow_data.update(session=session, openrouter=client, config=cfg, owner_ids=cfg.owner_ids)

    await bot.delete_webhook(drop_pending_updates=False)
    await bot.set_my_commands(COMMANDS)
    scheduler = asyncio.create_task(_dev_scheduler(bot, session, client))
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query", "my_chat_member"])
    finally:
        scheduler.cancel()
        await session.close()
        await bot.session.close()
        db.close()


def main() -> None:
    cfg = config_mod.load()
    db.connect(cfg.db_path)
    if db.get("openrouter_key") == "" and cfg.openrouter_key:
        db.put("openrouter_key", cfg.openrouter_key)

    if cfg.polling:
        log.info("starting in polling mode (development)")
        with suppress(KeyboardInterrupt, SystemExit):
            asyncio.run(_run_polling(cfg))
        return

    log.info("starting webhook server on :%s", cfg.port)
    web.run_app(
        build_app(cfg),
        host="0.0.0.0",
        port=cfg.port,
        handle_signals=True,
        access_log=None,
        print=None,
    )


if __name__ == "__main__":
    main()
