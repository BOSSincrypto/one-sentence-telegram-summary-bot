"""SQLite storage.

Deliberately dependency-free: one connection, WAL mode, plain SQL. At this
scale (tens of channels, a couple of digests per day) every query is
sub-millisecond, so there is no reason to pay for an ORM or an async driver.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA_VERSION = 1

_conn: sqlite3.Connection | None = None

# Runtime-editable settings. Everything here is exposed in the admin UI.
DEFAULTS: dict[str, Any] = {
    # --- schedule -----------------------------------------------------------
    "digest_time": "09:00",  # HH:MM in `tz`
    "tz": "Europe/Moscow",
    "window_hours": 24,
    # --- AI -----------------------------------------------------------------
    "openrouter_key": "",
    "models": [],  # fallback chain, tried in order
    "sentence_max": 160,  # max chars of one summary sentence
    "truncate": 1200,  # post chars sent to the model
    "short_verbatim": 80,  # shorter posts are used as-is, no LLM call
    "budget_usd": 0.0,  # monthly cap, 0 = unlimited
    # --- selection ----------------------------------------------------------
    "max_posts": 25,  # detailed items per channel
    "overflow": True,  # collapse the rest into "также писали про"
    "overflow_max": 25,
    "skip_media_only": True,
    "skip_ads": True,
    "dedupe": True,
    "dedupe_dist": 3,  # max Hamming distance treated as the same story
    "no_repeat": True,  # never send a post twice to the same destination
    # --- rendering ----------------------------------------------------------
    "collapse_at": 6,  # posts per channel before folding into a quote
    "show_time": False,
    "show_views": False,
    # --- ops ----------------------------------------------------------------
    "notify_errors": True,
    "concurrency": 3,  # parallel t.me fetches
    "max_pages": 8,  # t.me pages scanned per channel (20 posts each)
    "cron_job_id": 0,  # cron-job.org job managed by the bot
}

_DDL = """
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channel (
    id          INTEGER PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    fail_count  INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT NOT NULL DEFAULT '',
    last_ok_at  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS grp (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL UNIQUE,
    emoji     TEXT NOT NULL DEFAULT '',
    enabled   INTEGER NOT NULL DEFAULT 1,
    chat_id   INTEGER,
    thread_id INTEGER,
    dest_name TEXT NOT NULL DEFAULT '',
    model     TEXT NOT NULL DEFAULT '',      -- '' = use global chain
    max_posts INTEGER,                       -- NULL = use global
    kw_allow  TEXT NOT NULL DEFAULT '[]',
    kw_deny   TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS grp_channel (
    group_id   INTEGER NOT NULL REFERENCES grp(id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL REFERENCES channel(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, channel_id)
);

CREATE TABLE IF NOT EXISTS topic (
    chat_id    INTEGER NOT NULL,
    thread_id  INTEGER NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    chat_title TEXT NOT NULL DEFAULT '',
    ts         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, thread_id)
);

-- Summaries are cached by content hash, so re-running a digest (or a manual
-- preview followed by the scheduled run) costs nothing.
CREATE TABLE IF NOT EXISTS summary (
    k  TEXT PRIMARY KEY,
    s  TEXT NOT NULL,
    ad INTEGER NOT NULL DEFAULT 0,
    ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sent (
    group_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    post_id    INTEGER NOT NULL,
    ts         INTEGER NOT NULL,
    PRIMARY KEY (group_id, channel_id, post_id)
);

CREATE TABLE IF NOT EXISTS usage (
    day   TEXT NOT NULL,
    model TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    pt    INTEGER NOT NULL DEFAULT 0,
    ct    INTEGER NOT NULL DEFAULT 0,
    cost  REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, model)
);

CREATE TABLE IF NOT EXISTS run (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    group_id INTEGER NOT NULL DEFAULT 0,
    ok       INTEGER NOT NULL DEFAULT 1,
    posts    INTEGER NOT NULL DEFAULT 0,
    cost     REAL NOT NULL DEFAULT 0,
    ms       INTEGER NOT NULL DEFAULT 0,
    err      TEXT NOT NULL DEFAULT ''
);

-- The machine sleeps between button taps, so conversation state cannot live in
-- process memory: it would be lost the moment Fly stops the Machine.
CREATE TABLE IF NOT EXISTS fsm (
    key   TEXT PRIMARY KEY,
    state TEXT,
    data  TEXT NOT NULL DEFAULT '{}',
    ts    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS fsm_ts ON fsm(ts);
CREATE INDEX IF NOT EXISTS sent_ts ON sent(ts);
CREATE INDEX IF NOT EXISTS summary_ts ON summary(ts);
CREATE INDEX IF NOT EXISTS run_ts ON run(ts);
"""


def connect(path: str) -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_DDL)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    _conn = conn
    return conn


def db() -> sqlite3.Connection:
    if _conn is None:  # pragma: no cover - guarded by startup order
        raise RuntimeError("database is not connected")
    return _conn


def close() -> None:
    global _conn
    if _conn is not None:
        _conn.execute("PRAGMA optimize")
        _conn.close()
        _conn = None


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #

_cache: dict[str, Any] = {}


def get(key: str) -> Any:
    if key in _cache:
        return _cache[key]
    row = db().execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    value = json.loads(row["v"]) if row else DEFAULTS.get(key)
    _cache[key] = value
    return value


def put(key: str, value: Any) -> None:
    db().execute(
        "INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, json.dumps(value, ensure_ascii=False)),
    )
    _cache[key] = value


def settings() -> dict[str, Any]:
    return {k: get(k) for k in DEFAULTS}


def reset_cache() -> None:
    _cache.clear()


def local_date() -> date:
    """Today in the configured timezone.

    Spend is capped per calendar month, and "month" has to mean the owner's
    month — recording usage against a UTC date would roll the budget over at
    the wrong moment for anyone east or west of Greenwich.
    """
    try:
        zone = ZoneInfo(str(get("tz")))
    except (ZoneInfoNotFoundError, ValueError):
        zone = UTC
    return datetime.now(zone).date()


# --------------------------------------------------------------------------- #
# channels
# --------------------------------------------------------------------------- #


def add_channel(username: str, title: str = "") -> int:
    cur = db().execute(
        "INSERT INTO channel(username, title) VALUES(?, ?) "
        "ON CONFLICT(username) DO UPDATE SET title=COALESCE(NULLIF(excluded.title,''), channel.title) "
        "RETURNING id",
        (username, title),
    )
    return int(cur.fetchone()["id"])


def channels(enabled_only: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM channel"
    if enabled_only:
        sql += " WHERE enabled=1"
    return list(db().execute(sql + " ORDER BY LOWER(username)"))


def channel(channel_id: int) -> sqlite3.Row | None:
    return db().execute("SELECT * FROM channel WHERE id=?", (channel_id,)).fetchone()


def delete_channel(channel_id: int) -> None:
    db().execute("DELETE FROM channel WHERE id=?", (channel_id,))


def set_channel_enabled(channel_id: int, enabled: bool) -> None:
    db().execute("UPDATE channel SET enabled=? WHERE id=?", (int(enabled), channel_id))


def mark_channel_ok(channel_id: int, title: str) -> None:
    db().execute(
        "UPDATE channel SET fail_count=0, last_error='', last_ok_at=?, "
        "title=COALESCE(NULLIF(?,''), title) WHERE id=?",
        (int(time.time()), title, channel_id),
    )


def mark_channel_fail(channel_id: int, error: str) -> int:
    cur = db().execute(
        "UPDATE channel SET fail_count=fail_count+1, last_error=? WHERE id=? RETURNING fail_count",
        (error[:200], channel_id),
    )
    row = cur.fetchone()
    return int(row["fail_count"]) if row else 0


# --------------------------------------------------------------------------- #
# groups
# --------------------------------------------------------------------------- #


def add_group(name: str, emoji: str = "") -> int:
    cur = db().execute("INSERT INTO grp(name, emoji) VALUES(?, ?) RETURNING id", (name, emoji))
    return int(cur.fetchone()["id"])


def groups(enabled_only: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM grp"
    if enabled_only:
        sql += " WHERE enabled=1 AND chat_id IS NOT NULL"
    return list(db().execute(sql + " ORDER BY id"))


def group(group_id: int) -> sqlite3.Row | None:
    return db().execute("SELECT * FROM grp WHERE id=?", (group_id,)).fetchone()


def update_group(group_id: int, **fields: Any) -> None:
    allowed = {
        "name",
        "emoji",
        "enabled",
        "chat_id",
        "thread_id",
        "dest_name",
        "model",
        "max_posts",
        "kw_allow",
        "kw_deny",
    }
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    clause = ", ".join(f"{k}=?" for k in sets)
    db().execute(f"UPDATE grp SET {clause} WHERE id=?", (*sets.values(), group_id))


def delete_group(group_id: int) -> None:
    db().execute("DELETE FROM grp WHERE id=?", (group_id,))


def group_channels(group_id: int) -> list[sqlite3.Row]:
    return list(
        db().execute(
            "SELECT c.* FROM channel c JOIN grp_channel g ON g.channel_id=c.id "
            "WHERE g.group_id=? ORDER BY LOWER(c.username)",
            (group_id,),
        )
    )


def group_channel_ids(group_id: int) -> set[int]:
    return {
        int(r["channel_id"])
        for r in db().execute("SELECT channel_id FROM grp_channel WHERE group_id=?", (group_id,))
    }


def toggle_group_channel(group_id: int, channel_id: int) -> bool:
    """Returns True if the channel ended up attached to the group."""
    cur = db().execute(
        "DELETE FROM grp_channel WHERE group_id=? AND channel_id=?",
        (group_id, channel_id),
    )
    if cur.rowcount:
        return False
    db().execute(
        "INSERT INTO grp_channel(group_id, channel_id) VALUES(?, ?)",
        (group_id, channel_id),
    )
    return True


def groups_using_channel(channel_id: int) -> list[str]:
    return [
        r["name"]
        for r in db().execute(
            "SELECT g.name FROM grp g JOIN grp_channel gc ON gc.group_id=g.id "
            "WHERE gc.channel_id=? ORDER BY g.name",
            (channel_id,),
        )
    ]


# --------------------------------------------------------------------------- #
# forum topics (discovered from updates, or bound explicitly with /bind)
# --------------------------------------------------------------------------- #


def remember_topic(chat_id: int, thread_id: int, title: str, chat_title: str) -> None:
    db().execute(
        "INSERT INTO topic(chat_id, thread_id, title, chat_title, ts) VALUES(?,?,?,?,?) "
        "ON CONFLICT(chat_id, thread_id) DO UPDATE SET "
        "title=COALESCE(NULLIF(excluded.title,''), topic.title), "
        "chat_title=COALESCE(NULLIF(excluded.chat_title,''), topic.chat_title), ts=excluded.ts",
        (chat_id, thread_id, title, chat_title, int(time.time())),
    )


def topics() -> list[sqlite3.Row]:
    return list(db().execute("SELECT * FROM topic ORDER BY chat_title, thread_id"))


def topic(chat_id: int, thread_id: int) -> sqlite3.Row | None:
    return (
        db()
        .execute("SELECT * FROM topic WHERE chat_id=? AND thread_id=?", (chat_id, thread_id))
        .fetchone()
    )


def forget_topic(chat_id: int, thread_id: int) -> None:
    db().execute("DELETE FROM topic WHERE chat_id=? AND thread_id=?", (chat_id, thread_id))


# --------------------------------------------------------------------------- #
# summary cache / delivery bookkeeping
# --------------------------------------------------------------------------- #


def cached_summaries(keys: Iterable[str]) -> dict[str, tuple[str, bool]]:
    keys = list(keys)
    if not keys:
        return {}
    out: dict[str, tuple[str, bool]] = {}
    for i in range(0, len(keys), 400):  # stay well under SQLITE_MAX_VARIABLE_NUMBER
        chunk = keys[i : i + 400]
        marks = ",".join("?" * len(chunk))
        for row in db().execute(f"SELECT k, s, ad FROM summary WHERE k IN ({marks})", chunk):
            out[row["k"]] = (row["s"], bool(row["ad"]))
    return out


def store_summaries(items: Iterable[tuple[str, str, bool]]) -> None:
    now = int(time.time())
    db().executemany(
        "INSERT INTO summary(k, s, ad, ts) VALUES(?,?,?,?) "
        "ON CONFLICT(k) DO UPDATE SET s=excluded.s, ad=excluded.ad, ts=excluded.ts",
        [(k, s, int(ad), now) for k, s, ad in items],
    )


def clear_summary_cache() -> int:
    cur = db().execute("DELETE FROM summary")
    return cur.rowcount or 0


def already_sent(group_id: int, pairs: Iterable[tuple[int, int]]) -> set[tuple[int, int]]:
    pairs = list(pairs)
    if not pairs:
        return set()
    out: set[tuple[int, int]] = set()
    for i in range(0, len(pairs), 200):
        chunk = pairs[i : i + 200]
        marks = ",".join("(?,?)" for _ in chunk)
        flat = [x for pair in chunk for x in pair]
        rows = db().execute(
            f"SELECT channel_id, post_id FROM sent WHERE group_id=? "
            f"AND (channel_id, post_id) IN ({marks})",
            (group_id, *flat),
        )
        out.update((int(r["channel_id"]), int(r["post_id"])) for r in rows)
    return out


def mark_sent(group_id: int, pairs: Iterable[tuple[int, int]]) -> None:
    now = int(time.time())
    db().executemany(
        "INSERT OR IGNORE INTO sent(group_id, channel_id, post_id, ts) VALUES(?,?,?,?)",
        [(group_id, c, p, now) for c, p in pairs],
    )


# --------------------------------------------------------------------------- #
# usage & run log
# --------------------------------------------------------------------------- #


def add_usage(day: str, model: str, pt: int, ct: int, cost: float) -> None:
    db().execute(
        "INSERT INTO usage(day, model, calls, pt, ct, cost) VALUES(?,?,1,?,?,?) "
        "ON CONFLICT(day, model) DO UPDATE SET calls=usage.calls+1, pt=usage.pt+excluded.pt, "
        "ct=usage.ct+excluded.ct, cost=usage.cost+excluded.cost",
        (day, model, pt, ct, cost),
    )


def usage_since(day: str) -> list[sqlite3.Row]:
    return list(
        db().execute(
            "SELECT model, SUM(calls) calls, SUM(pt) pt, SUM(ct) ct, SUM(cost) cost "
            "FROM usage WHERE day>=? GROUP BY model ORDER BY cost DESC",
            (day,),
        )
    )


def month_cost(month_prefix: str) -> float:
    row = (
        db()
        .execute(
            "SELECT COALESCE(SUM(cost), 0) c FROM usage WHERE day LIKE ?", (month_prefix + "%",)
        )
        .fetchone()
    )
    return float(row["c"])


def log_run(group_id: int, ok: bool, posts: int, cost: float, ms: int, err: str = "") -> None:
    db().execute(
        "INSERT INTO run(ts, group_id, ok, posts, cost, ms, err) VALUES(?,?,?,?,?,?,?)",
        (int(time.time()), group_id, int(ok), posts, cost, ms, err[:300]),
    )


def recent_runs(limit: int = 10) -> list[sqlite3.Row]:
    return list(db().execute("SELECT * FROM run ORDER BY id DESC LIMIT ?", (limit,)))


def prune(now: int | None = None) -> None:
    """Keep the database small enough to never matter on a 1 GB volume."""
    now = now or int(time.time())
    db().execute("DELETE FROM sent WHERE ts < ?", (now - 3 * 86400,))
    db().execute("DELETE FROM summary WHERE ts < ?", (now - 7 * 86400,))
    db().execute("DELETE FROM usage WHERE day < date('now', '-90 day')")
    db().execute("DELETE FROM run WHERE id NOT IN (SELECT id FROM run ORDER BY id DESC LIMIT 50)")
    db().execute("DELETE FROM fsm WHERE ts < ?", (now - 86400,))
