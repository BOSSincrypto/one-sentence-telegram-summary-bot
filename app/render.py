"""Rendering a :class:`~app.digest.Digest` into Telegram HTML messages.

Layout: the clickable target is the compact index number, so the summary itself
stays plain, unstyled text and the eye reads a column of sentences rather than a
wall of blue underline. A channel with many posts folds into an expandable
blockquote, which keeps a 20-channel digest to one screen and doubles as the
answer to the 4096-character message limit.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import db
from .digest import Digest, Line

LIMIT = 4096
SAFE = 3900  # headroom for the entity overhead Telegram counts separately

_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def tz() -> ZoneInfo:
    try:
        return ZoneInfo(str(db.get("tz")))
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def esc(text: str) -> str:
    return escape(text, quote=False)


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def human_views(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{n / 1_000:.1f}K".replace(".0K", "K")
    return str(n)


def _line(index: int, line: Line, *, show_time: bool, show_views: bool, zone: ZoneInfo) -> str:
    bits = [f'<a href="{escape(line.link, quote=True)}">{index}</a> · ']
    if show_time:
        bits.append(f"<code>{datetime.fromtimestamp(line.ts, zone):%H:%M}</code> ")
    bits.append(esc(line.summary))
    tail = []
    if show_views and line.views:
        tail.append(f"👁 {human_views(line.views)}")
    if line.also_in:
        tail.append("также: " + esc(", ".join(line.also_in)))
    if tail:
        bits.append(f" <i>· {' · '.join(tail)}</i>")
    return "".join(bits)


def _block_lines(block, *, show_time: bool, show_views: bool, zone: ZoneInfo) -> list[str]:
    lines = [
        _line(i, line, show_time=show_time, show_views=show_views, zone=zone)
        for i, line in enumerate(block.items, 1)
    ]
    if block.extra:
        topics = ", ".join(
            f'<a href="{escape(x.link, quote=True)}">{esc(x.summary.rstrip("."))}</a>'
            for x in block.extra
        )
        lines.append(f"<i>Также писали про:</i> {topics}")
    if block.hidden:
        lines.append(
            f"<i>…и ещё {block.hidden} {plural(block.hidden, 'пост', 'поста', 'постов')}</i>"
        )
    return lines


def _wrap(title: str, lines: list[str], collapse: bool) -> str:
    body = "\n".join(lines)
    if collapse and body:
        body = f"<blockquote expandable>{body}</blockquote>"
    return f"{title}\n{body}" if body else title


def _pack(header: str, chunks: list[tuple[str, list[str], bool]]) -> list[str]:
    """Greedily packs channel blocks into messages under the size limit.

    A block that does not fit even alone is split line by line, and each part
    repeats the channel title (and re-opens its blockquote) so no message
    arrives without a heading.
    """
    messages: list[str] = []
    current = header

    def flush() -> None:
        nonlocal current
        if current.strip():
            messages.append(current)
        current = ""

    for title, lines, collapse in chunks:
        text = _wrap(title, lines, collapse)
        if len(text) <= SAFE and len(current) + len(text) + 2 <= SAFE:
            current = f"{current}\n\n{text}" if current else text
            continue
        flush()
        if len(text) <= SAFE:
            current = text
            continue

        part: list[str] = []
        for line in lines:
            candidate = _wrap(title, [*part, line], collapse)
            if part and len(candidate) > SAFE:
                messages.append(_wrap(title, part, collapse))
                part = [line]
            else:
                part.append(line)
        if part:
            current = _wrap(title, part, collapse)
    flush()
    return messages or [header]


def render(digest: Digest, *, verbose: bool = False) -> list[str]:
    settings = db.settings()
    zone = tz()
    show_time = bool(settings["show_time"])
    show_views = bool(settings["show_views"])
    collapse_at = int(settings["collapse_at"])

    end = datetime.fromtimestamp(digest.window_end, zone)
    total = digest.total
    channels = len(digest.blocks)
    title = " ".join(p for p in (digest.group_emoji.strip(), digest.group_name) if p)
    header = (
        f"📰 <b>{esc(title)}</b>\n"
        f"<i>{end.day} {_MONTHS[end.month - 1]}, {end:%H:%M} · за "
        f"{int(settings['window_hours'])} ч · {channels} "
        f"{plural(channels, 'канал', 'канала', 'каналов')} · {total} "
        f"{plural(total, 'пост', 'поста', 'постов')}</i>"
    )

    if not digest.blocks:
        body = "\n\n<i>За выбранный период нечего показать.</i>"
        if digest.errors and verbose:
            body += "\n\n⚠️ " + "\n⚠️ ".join(esc(e) for e in digest.errors)
        return [header + body]

    chunks: list[tuple[str, list[str], bool]] = []
    for block in digest.blocks:
        count = len(block.items)
        head = f"📣 <b>{esc(block.title)}</b>"
        if count:
            head += f" · {count}"
        lines = _block_lines(block, show_time=show_time, show_views=show_views, zone=zone)
        chunks.append((head, lines, count > collapse_at))

    messages = _pack(header, chunks)

    footer_bits: list[str] = []
    if digest.deduped:
        footer_bits.append(
            f"{digest.deduped} {plural(digest.deduped, 'дубль свёрнут', 'дубля свёрнуто', 'дублей свёрнуто')}"
        )
    if digest.skipped_ads:
        footer_bits.append(f"реклама скрыта: {digest.skipped_ads}")
    if verbose:
        if digest.models:
            footer_bits.append("модель: " + esc(", ".join(digest.models)))
        footer_bits.append(f"${digest.cost:.4f}")
    if footer_bits:
        footer = f"\n\n<i>{' · '.join(footer_bits)}</i>"
        if len(messages[-1]) + len(footer) <= SAFE:
            messages[-1] += footer
        else:
            messages.append(footer.strip())

    if verbose and digest.errors:
        note = "\n\n⚠️ " + "\n⚠️ ".join(esc(e) for e in digest.errors)
        if len(messages[-1]) + len(note) <= SAFE:
            messages[-1] += note
        else:
            messages.append(note.strip())

    return messages
