"""Digest assembly: collect → filter → dedupe → rank → summarise.

Pure logic — nothing here talks to Telegram. The order of the stages is chosen
so that every token spent on the model is spent on a post that will actually be
shown: cheap local filters and cross-channel deduplication run first, the model
runs last, and anything already summarised comes from the cache.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field

from aiohttp import ClientSession

from . import db, dedupe, tme
from .llm import BudgetExceeded, Item, LLMError, OpenRouter

CACHE_VERSION = "v1"

# Obvious promotional markers, matched before the model sees the post.
_AD_PATTERNS = re.compile(
    r"(?:^|\W)(?:#реклама|реклама\b|рекламодател|на правах рекламы|"
    r"партнёрск\w+ материал|партнерск\w+ материал|промокод|erid\s*[:=]|"
    r"по промокоду|скидк\w+ по коду|#промо|#ad\b|sponsored)",
    re.IGNORECASE,
)

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s")


@dataclass(slots=True)
class Line:
    channel_id: int
    channel: str
    channel_title: str
    post_id: int
    ts: int
    views: int
    text: str
    full: bool
    key: str = ""
    summary: str = ""
    is_ad: bool = False
    also_in: list[str] = field(default_factory=list)

    @property
    def link(self) -> str:
        return f"https://t.me/{self.channel}/{self.post_id}"


@dataclass(slots=True)
class Block:
    channel_id: int
    username: str
    title: str
    items: list[Line] = field(default_factory=list)
    extra: list[Line] = field(default_factory=list)
    hidden: int = 0


@dataclass(slots=True)
class Digest:
    group_id: int
    group_name: str
    group_emoji: str
    blocks: list[Block]
    window_start: int
    window_end: int
    cost: float = 0.0
    models: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    deduped: int = 0
    skipped_ads: int = 0

    @property
    def total(self) -> int:
        return sum(len(b.items) + len(b.extra) for b in self.blocks)

    @property
    def pairs(self) -> list[tuple[int, int]]:
        return [
            (line.channel_id, line.post_id)
            for block in self.blocks
            for line in (*block.items, *block.extra)
        ]


def looks_like_ad(text: str) -> bool:
    return bool(_AD_PATTERNS.search(text))


def cache_key(text: str, full: bool, limit: int) -> str:
    payload = f"{CACHE_VERSION}|{'F' if full else 'T'}|{limit}|{dedupe.normalize(text)}"
    return hashlib.blake2b(payload.encode(), digest_size=12).hexdigest()


def first_sentence(text: str, limit: int) -> str:
    """Fallback used when the model omits an item — never leaves a hole."""
    flat = " ".join(text.split())
    head = _SENTENCE_END.split(flat, maxsplit=1)[0]
    if len(head) <= limit:
        return head
    cut = head[:limit].rsplit(" ", 1)[0]
    return (cut or head[:limit]).rstrip(" ,;:—-") + "…"


def _keywords(raw: str) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [str(x) for x in value if str(x).strip()] if isinstance(value, list) else []


async def _collect(
    session: ClientSession,
    channels: list[sqlite3.Row],
    since: int,
    concurrency: int,
    max_pages: int,
) -> tuple[dict[int, tuple[str, list[tme.Post]]], list[str]]:
    semaphore = asyncio.Semaphore(max(1, concurrency))
    errors: list[str] = []
    out: dict[int, tuple[str, list[tme.Post]]] = {}

    async def one(row: sqlite3.Row) -> None:
        async with semaphore:
            try:
                title, posts = await tme.fetch_posts(
                    session, row["username"], since, max_pages=max_pages
                )
            except tme.ChannelError as exc:
                db.mark_channel_fail(int(row["id"]), str(exc))
                errors.append(f"@{row['username']}: {exc}")
                return
            except Exception as exc:  # pragma: no cover - defensive
                db.mark_channel_fail(int(row["id"]), repr(exc))
                errors.append(f"@{row['username']}: {type(exc).__name__}")
                return
            db.mark_channel_ok(int(row["id"]), title)
            out[int(row["id"])] = (title or row["username"], posts)

    await asyncio.gather(*(one(row) for row in channels))
    return out, errors


def _select(
    row: sqlite3.Row,
    title: str,
    posts: list[tme.Post],
    *,
    settings: dict,
    allow: list[str],
    deny: list[str],
    seen: set[tuple[int, int]],
    max_posts: int,
) -> Block:
    channel_id = int(row["id"])
    block = Block(channel_id=channel_id, username=row["username"], title=title)

    candidates: list[tme.Post] = []
    for post in posts:
        if (channel_id, post.id) in seen:
            continue
        if not post.text:
            if settings["skip_media_only"]:
                continue
        else:
            if settings["skip_ads"] and looks_like_ad(post.text):
                continue
            if deny and dedupe.any_match(post.text, deny):
                continue
            if allow and not dedupe.any_match(post.text, allow):
                continue
        candidates.append(post)

    # Reach is the only quality signal available for free from the preview page.
    candidates.sort(key=lambda p: (-p.views, -p.ts))
    detailed = candidates[:max_posts]

    if settings["overflow"]:
        overflow = candidates[max_posts : max_posts + int(settings["overflow_max"])]
        block.hidden = max(0, len(candidates) - max_posts - len(overflow))
    else:
        overflow = []
        block.hidden = max(0, len(candidates) - max_posts)

    make = lambda post, full: Line(  # noqa: E731 - local shorthand, one shape
        channel_id=channel_id,
        channel=row["username"],
        channel_title=title,
        post_id=post.id,
        ts=post.ts,
        views=post.views,
        text=post.text,
        full=full,
    )
    block.items = [make(p, True) for p in detailed]
    block.extra = [make(p, False) for p in overflow]
    return block


async def _summarise(
    client: OpenRouter,
    blocks: list[Block],
    chain: list[str],
    settings: dict,
) -> tuple[float, list[str], list[str]]:
    limit = int(settings["sentence_max"])
    verbatim = int(settings["short_verbatim"])
    cost = 0.0
    used: list[str] = []
    errors: list[str] = []

    pending: dict[int, list[tuple[Line, Item]]] = {}
    for block in blocks:
        for line in (*block.items, *block.extra):
            if not line.text:
                line.summary = "Медиа без подписи."
                continue
            if line.full and len(line.text) <= verbatim:
                # Already one short sentence — paying a model to shorten it
                # would cost more than it is worth.
                line.summary = " ".join(line.text.split())
                line.is_ad = looks_like_ad(line.text)
                continue
            line.key = cache_key(line.text, line.full, limit)
            pending.setdefault(block.channel_id, []).append(
                (line, Item(line.key, line.text, line.full))
            )

    cached = db.cached_summaries({item.key for entries in pending.values() for _, item in entries})
    fresh: list[tuple[str, str, bool]] = []

    for block in blocks:
        entries = pending.get(block.channel_id) or []
        todo: list[Item] = []
        seen_keys: set[str] = set()
        for line, item in entries:
            hit = cached.get(item.key)
            if hit:
                line.summary, line.is_ad = hit[0], hit[1]
            elif item.key not in seen_keys:
                seen_keys.add(item.key)
                todo.append(item)

        if todo:
            try:
                results, call_cost, model = await client.summarize(block.title, todo, chain)
                cost += call_cost
                if model and model not in used:
                    used.append(model)
                for line, item in entries:
                    result = results.get(item.key)
                    if result:
                        line.summary, line.is_ad = result.summary, result.is_ad
                        fresh.append((item.key, result.summary, result.is_ad))
            except BudgetExceeded as exc:
                errors.append(str(exc))
                break
            except LLMError as exc:
                errors.append(f"@{block.username}: {exc}")

        for line, _ in entries:
            if not line.summary:
                line.summary = first_sentence(line.text, limit)
                line.is_ad = looks_like_ad(line.text)

    if fresh:
        db.store_summaries(fresh)
    return cost, used, errors


async def build(
    session: ClientSession,
    client: OpenRouter,
    group: sqlite3.Row,
    *,
    respect_sent: bool = True,
) -> Digest:
    settings = db.settings()
    group_id = int(group["id"])
    channels = [row for row in db.group_channels(group_id) if row["enabled"]]

    now = int(time.time())
    since = now - int(settings["window_hours"]) * 3600
    digest = Digest(
        group_id=group_id,
        group_name=group["name"],
        group_emoji=group["emoji"] or "",
        blocks=[],
        window_start=since,
        window_end=now,
    )
    if not channels:
        digest.errors.append("В группе нет активных каналов.")
        return digest

    fetched, digest.errors = await _collect(
        session, channels, since, int(settings["concurrency"]), int(settings["max_pages"])
    )

    allow, deny = _keywords(group["kw_allow"]), _keywords(group["kw_deny"])
    max_posts = int(group["max_posts"] or settings["max_posts"])
    seen: set[tuple[int, int]] = set()
    if respect_sent and settings["no_repeat"]:
        candidate_pairs = [(cid, post.id) for cid, (_, posts) in fetched.items() for post in posts]
        seen = db.already_sent(group_id, candidate_pairs)

    blocks: list[Block] = []
    for row in channels:
        entry = fetched.get(int(row["id"]))
        if entry is None:
            continue
        title, posts = entry
        block = _select(
            row,
            title,
            posts,
            settings=settings,
            allow=allow,
            deny=deny,
            seen=seen,
            max_posts=max_posts,
        )
        if block.items or block.extra:
            blocks.append(block)

    if settings["dedupe"]:
        digest.deduped = _dedupe_across(blocks, int(settings["dedupe_dist"]))

    # A group-level model is a preference, not a replacement: the global chain
    # stays behind it so a rate-limited override still has somewhere to fall.
    chain = [m for m in (settings["models"] or []) if m]
    override = (group["model"] or "").strip()
    if override:
        chain = [override] + [m for m in chain if m != override]
    cost, models, llm_errors = await _summarise(client, blocks, chain, settings)
    digest.cost, digest.models = cost, models
    digest.errors.extend(llm_errors)

    if settings["skip_ads"]:
        for block in blocks:
            before = len(block.items) + len(block.extra)
            block.items = [line for line in block.items if not line.is_ad]
            block.extra = [line for line in block.extra if not line.is_ad]
            digest.skipped_ads += before - len(block.items) - len(block.extra)

    for block in blocks:
        block.items.sort(key=lambda line: line.ts)
        block.extra.sort(key=lambda line: line.ts)

    digest.blocks = [b for b in blocks if b.items or b.extra]
    return digest


def _dedupe_across(blocks: list[Block], max_distance: int) -> int:
    """Collapses the same story carried by several channels into one line."""
    pool = [line for block in blocks for line in block.items]
    if len(pool) < 2:
        return 0

    kept, absorbed = dedupe.dedupe(
        pool,
        text_of=lambda line: line.text,
        rank_of=lambda line: line.views,
        max_distance=max_distance,
    )
    if len(kept) == len(pool):
        return 0

    for position, dropped in absorbed.items():
        titles = {pool[i].channel_title for i in dropped} - {kept[position].channel_title}
        kept[position].also_in = sorted(titles)

    survivors = {id(line) for line in kept}
    for block in blocks:
        block.items = [line for line in block.items if id(line) in survivors]
    return len(pool) - len(kept)
