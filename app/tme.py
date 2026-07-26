"""Scraper for the public channel preview at ``https://t.me/s/<channel>``.

The preview is server-rendered HTML, so no API key, no phone number and no
JavaScript engine are involved. Parsing uses selectolax (Lexbor) rather than
BeautifulSoup/lxml: it is roughly an order of magnitude faster and allocates a
fraction of the memory, which matters on a 256 MB machine.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from aiohttp import ClientSession, ClientTimeout
from selectolax.lexbor import LexborHTMLParser, LexborNode

BASE = "https://t.me/s/"
PER_PAGE = 20
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0 Safari/537.36"
)
_TIMEOUT = ClientTimeout(total=25, connect=10)

_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_WS = re.compile(r"[ \t ]+")
_NL = re.compile(r"\n{3,}")
_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")

_MEDIA_SELECTORS = (
    ".tgme_widget_message_photo_wrap",
    ".tgme_widget_message_video_player",
    ".tgme_widget_message_roundvideo_player",
    ".tgme_widget_message_voice_player",
    ".tgme_widget_message_document",
    ".tgme_widget_message_sticker_wrap",
)


class ChannelError(Exception):
    """A channel could not be read; the message is shown to the owner as-is."""


@dataclass(slots=True)
class Post:
    id: int
    ts: int  # unix seconds, UTC
    text: str
    views: int
    has_media: bool
    channel: str

    @property
    def link(self) -> str:
        return f"https://t.me/{self.channel}/{self.id}"


def normalize_username(raw: str) -> str:
    """Accepts ``@name``, ``name``, ``t.me/name``, ``https://t.me/s/name``."""
    s = raw.strip()
    s = re.sub(r"^https?://", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^(www\.)?(t\.me|telegram\.me|telegram\.dog)/", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^s/", "", s, flags=re.IGNORECASE)
    s = s.split("/")[0].split("?")[0].lstrip("@").strip()
    if not _USERNAME.match(s):
        raise ChannelError(
            "Это не похоже на публичный канал. Нужен @username или ссылка вида t.me/name."
        )
    return s.lower()


def _views(raw: str) -> int:
    s = raw.strip().replace(" ", "").replace(" ", "").replace(",", ".")
    if not s:
        return 0
    mult = 1
    if s[-1] in "KkКк":
        mult, s = 1_000, s[:-1]
    elif s[-1] in "MmМм":
        mult, s = 1_000_000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def _clean(text: str) -> str:
    text = _WS.sub(" ", text.replace("\r", ""))
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _NL.sub("\n\n", text).strip()


def _node_text(node: LexborNode | None) -> str:
    return _clean(node.text(separator="")) if node is not None else ""


def _extract_text(msg: LexborNode) -> str:
    """Body text, falling back to a poll question or a link-preview blurb."""
    body = _node_text(msg.css_first(".tgme_widget_message_text.js-message_text"))
    if body:
        return body

    poll = _node_text(msg.css_first(".tgme_widget_message_poll_question"))
    if poll:
        return f"Опрос: {poll}"

    title = _node_text(msg.css_first(".link_preview_title"))
    desc = _node_text(msg.css_first(".link_preview_description"))
    joined = "\n".join(p for p in (title, desc) if p)
    return joined


def parse_page(html: str, channel: str) -> tuple[str, list[Post], int | None]:
    """Returns ``(channel_title, posts, before_cursor)`` for one preview page.

    ``before_cursor`` is Telegram's own "load older messages" anchor. Relying on
    it rather than on the number of posts per page matters: a page legitimately
    renders fewer than :data:`PER_PAGE` messages when some were deleted, and
    treating that as end-of-history silently truncates the digest.
    """
    tree = LexborHTMLParser(_BR.sub("\n", html))

    title_node = tree.css_first('meta[property="og:title"]')
    title = (title_node.attributes.get("content") or "").strip() if title_node else ""

    before: int | None = None
    more = tree.css_first("a.tme_messages_more[data-before]")
    if more is not None:
        raw_before = more.attributes.get("data-before") or ""
        if raw_before.isdigit():
            before = int(raw_before)

    posts: list[Post] = []
    for msg in tree.css(".tgme_widget_message[data-post]"):
        data_post = msg.attributes.get("data-post") or ""
        _, _, raw_id = data_post.rpartition("/")
        if not raw_id.isdigit():
            continue
        if msg.css_first(".service_message") is not None:
            continue

        time_node = msg.css_first(".tgme_widget_message_date time[datetime]")
        stamp = (time_node.attributes.get("datetime") or "") if time_node else ""
        if not stamp:
            continue
        try:
            dt = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)

        posts.append(
            Post(
                id=int(raw_id),
                ts=int(dt.timestamp()),
                text=_extract_text(msg),
                views=_views(_node_text(msg.css_first(".tgme_widget_message_views"))),
                has_media=any(msg.css_first(sel) is not None for sel in _MEDIA_SELECTORS),
                channel=channel,
            )
        )

    posts.sort(key=lambda p: p.id)
    return title, posts, before


async def _get(session: ClientSession, url: str) -> str:
    last: Exception | None = None
    for attempt in range(3):
        try:
            async with session.get(
                url,
                headers={"User-Agent": _UA, "Accept-Language": "ru,en;q=0.8"},
                timeout=_TIMEOUT,
                allow_redirects=False,
            ) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    # t.me bounces the preview URL to the join page when the
                    # channel is missing, private, or has previews disabled.
                    raise ChannelError(
                        "Канал не найден или у него отключён публичный предпросмотр "
                        "(в настройках канала должен быть включён «Показывать в поиске» / публичный доступ)."
                    )
                if resp.status == 429:
                    raise ChannelError("Telegram временно ограничил запросы (429). Попробую позже.")
                if resp.status != 200:
                    raise ChannelError(f"t.me ответил HTTP {resp.status}.")
                return await resp.text()
        except ChannelError:
            raise
        except (TimeoutError, OSError) as exc:  # transient network faults
            last = exc
            if attempt < 2:
                await asyncio.sleep(1.5 * (attempt + 1))
    raise ChannelError(f"Сеть недоступна: {type(last).__name__}")


async def fetch_posts(
    session: ClientSession,
    username: str,
    since_ts: int,
    max_pages: int = 8,
) -> tuple[str, list[Post]]:
    """Walks the preview backwards until it passes ``since_ts``.

    Returns the channel title and every post newer than ``since_ts``, oldest
    first. Paging stops as early as possible so a quiet channel costs exactly
    one HTTP request.
    """
    url = f"{BASE}{username}"
    title = ""
    collected: dict[int, Post] = {}

    for _ in range(max(1, max_pages)):
        page_title, posts, before = parse_page(await _get(session, url), username)
        title = title or page_title

        for post in posts:
            if post.ts >= since_ts:
                collected[post.id] = post

        # Stop as soon as the page reaches past the window, so a quiet channel
        # costs exactly one request.
        reached_window_edge = bool(posts) and posts[0].ts < since_ts
        if reached_window_edge or before is None or before <= 1:
            break
        url = f"{BASE}{username}?before={before}"

    return title, sorted(collected.values(), key=lambda p: p.ts)


async def probe(session: ClientSession, username: str) -> str:
    """Validates a channel and returns its title. Raises :class:`ChannelError`."""
    title, _, _ = parse_page(await _get(session, f"{BASE}{username}"), username)
    return title or username
