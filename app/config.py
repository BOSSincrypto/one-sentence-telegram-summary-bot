"""Process-level configuration: only things that must exist before the DB opens.

Everything a human might want to change at runtime lives in the database and is
editable from the bot's admin UI (see :mod:`app.db`). Env vars here are limited
to secrets and deployment wiring.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _owner_ids() -> frozenset[int]:
    raw = _env("OWNER_IDS") or _env("OWNER_ID")
    out: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.lstrip("-").isdigit():
            out.add(int(chunk))
    return frozenset(out)


@dataclass(frozen=True, slots=True)
class Config:
    bot_token: str
    owner_ids: frozenset[int]
    db_path: str
    base_url: str
    webhook_secret: str
    tick_key: str
    openrouter_key: str
    cronjob_key: str
    port: int
    polling: bool

    @property
    def webhook_path(self) -> str:
        return f"/tg/{self.webhook_secret}"

    @property
    def webhook_url(self) -> str:
        return f"{self.base_url}{self.webhook_path}"

    @property
    def tick_url(self) -> str:
        return f"{self.base_url}/tick?key={self.tick_key}"


def load() -> Config:
    token = _env("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN is required")

    owners = _owner_ids()
    if not owners:
        raise SystemExit("OWNER_IDS is required (your numeric Telegram user id)")

    base = _env("BASE_URL").rstrip("/")
    app_name = _env("FLY_APP_NAME")
    if not base and app_name:
        base = f"https://{app_name}.fly.dev"

    # Deterministic per-deployment secrets so a restart does not invalidate the
    # webhook that Telegram already knows about.
    seed = token.split(":")[-1]
    return Config(
        bot_token=token,
        owner_ids=owners,
        db_path=_env("DB_PATH", "/data/bot.db"),
        base_url=base,
        webhook_secret=_env("WEBHOOK_SECRET") or seed[:24] or secrets.token_urlsafe(16),
        tick_key=_env("TICK_KEY") or seed[-24:] or secrets.token_urlsafe(16),
        openrouter_key=_env("OPENROUTER_API_KEY"),
        cronjob_key=_env("CRONJOB_API_KEY"),
        port=int(_env("PORT", "8080")),
        # Long polling is for local development only; on Fly the machine sleeps
        # and is woken by inbound HTTP, which requires a webhook.
        polling=_env("POLLING", "").lower() in {"1", "true", "yes"} or not base,
    )
