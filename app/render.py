"""Rendering a :class:`~app.digest.Digest` into Telegram HTML messages.

Each post line is built like this::

    ① Первое предложение саммари.
       ↗ Открыть

The index "①" is the visible, tappable target — bold, underlined, and bigger
than the surrounding text. The "↗ Открыть" button-style link is a second tap
target on the right, so the eye can land on either one. The summary itself
stays as plain text so the column still reads as sentences, not as a wall of
underlined links. Channels with many posts fold into an expandable quote, which
keeps a 20-channel digest to one screen and doubles as the answer to Telegram's
4096-character message limit.
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


def _chip_label(index: int) -> str:
    """A short, visually distinctive label for the post number.

    Telegram renders these circled integer glyphs in a single fixed-width slot
    that is much easier to hit with a thumb than a plain digit floating at
    the start of a sentence. They render inside a clickable HTML anchor just
    like regular text, so the link target is the glyph itself.
    """
    circled = (
        "\u2460", "\u2461", "\u2462", "\u2463", "\u2464",
        "\u2465", "\u2466", "\u2467", "\u2468", "\u2469",
        "\u246a", "\u246b", "\u246c", "\u246d", "\u246e",
        "\u246f", "\u2470", "\u2471", "\u2472", "\u2473",
    )
    # Support for post indices beyond circled digits
    if 1 <= index <= len(circled):
        return circled[index - 1]
    return f"{index}\ufe0f\u20e3"


def _line(index: int, line: Line, *, show_time: bool, show_views: bool, zone: ZoneInfo) -> str:
    href = escape(line.link, quote=True)
    label = _chip_label(index)

    meta_bits: list[str] = []
    if show_time:
        meta_bits.append(f"<code>{datetime.fromtimestamp(line.ts, zone):%H:%M}</code>")
    if show_views and line.views:
        meta_bits.append(f"\U0001F441 {human_views(line.views)}")
    if line.also_in:
        meta_bits.append("также: " + esc(", ".join(line.also_in)))

    meta = (" <i>" + " · ".join(meta_bits) + "</i>") if meta_bits else ""

    return (
        f'<b><a href="{href}">{label}</a></b> {esc(line.summary)}'
        f'<a href="{href}"> \u2197 Открыть</a>{meta}'
    )


def _block_lines(block, *, show_time: bool, show_views: bool, zone: ZoneInfo) -> list[str]:
    lines = [
        _line(i, line, show_time=show_time, show_views=show_views, zone=zone)
        for i, line in enumerate(block.items, 1)
    ]
    if block.extra:
        topics = ", ".join(
            f'<b><a href="{escape(x.link, quote=True)}">{esc(x.summary.rstrip("."))}</a></b>'
            for x in block.extra
        )
        lines.append(f"<i>Также писали про:</i> {topics}")
    if block.hidden:
        lines.append(
            f"<i>\u2026и ещё {block.hidden} {plural(block.hidden, 'пост', 'поста', 'постов')}</i>"
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
        f"\U0001F4F0 <b>{esc(title)}</b>\n"
        f"<i>{end.day} {_MONTHS[end.month - 1]}, {end:%H:%M} \u00b7 за "
        f"{int(settings['window_hours'])} ч \u00b7 {channels} "
        f"{plural(channels, 'канал', 'канала', 'каналов')} \u00b7 {total} "
        f"{plural(total, 'пост', 'поста', 'постов')}</i>"
    )

    if not digest.blocks:
        body = "\n\n<i>За выбранный период нечего показать.</i>"
        if digest.errors and verbose:
            body += "\n\n\u26A0\ufe0f " + "\n\u26A0\ufe0f ".join(esc(e) for e in digest.errors)
        return [header + body]

    chunks: list[tuple[str, list[str], bool]] = []
    for block in digest.blocks:
        count = len(block.items)
        head = f"\U0001F4E3 <b>{esc(block.title)}</b>"
        if count:
            head += f" \u00b7 {count}"
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
        footer = f"\n\n<i>{' \u00b7 '.join(footer_bits)}</i>"
        if len(messages[-1]) + len(footer) <= SAFE:
            messages[-1] += footer
        else:
            messages.append(footer.strip())

    if verbose and digest.errors:
        note = "\n\n\u26A0\ufe0f " + "\n\u26A0\ufe0f ".join(esc(e) for e in digest.errors)
        if len(messages[-1]) + len(note) <= SAFE:
            messages[-1] += note
        else:
            messages.append(note.strip())

    return messages


def render_channel(digest: Digest, dest: str = "", *, verbose: bool = False) -> list[str]:
    """Renders a flat ciscrypted-style digest for a public channel.

    One line per post — ``<b>Source:</b> <a>short label</a>`` — no chips,
    no «Открыть», no views/time meta, no expandable quotes. The flat list
    is already ranked and truncated by :func:`app.digest.build_channel`,
    so this only orders and packs.
    """
    zone = tz()
    end = datetime.fromtimestamp(digest.window_end, zone)
    total = sum(len(b.items) for b in digest.blocks)
    header = (
        f"\u26A1 <b>CRYPTO \u0412\u042B\u0416\u0418\u041C\u041A\u0410 \u2014 "
        f"{end.day} {_MONTHS[end.month - 1]}</b>\n"
        f"<i>\u0437\u0430 24 \u0447 \u00b7 {total} "
        f"{plural(total, 'пост', 'поста', 'постов')} \u00b7 22:00 \u041C\u0421\u041A</i>"
    )

    handle = (dest or "").strip().lstrip("@").split("/")[0]
    if handle and handle.startswith("-100"):
        footer_link = ""
    elif handle:
        footer_link = (
            "\n\n<b>CRYPTO \u0412\u042B\u0416\u0418\u041C\u041A\u0410</b> \u2014 "
            f'<a href="https://t.me/{esc(handle)}">\u041F\u043E\u0434\u043F\u0438\u0448\u0438\u0441\u044C / '
            "\u043F\u043E\u0434\u0435\u043B\u0438\u0441\u044C</a>"
        )
    else:
        footer_link = "\n\n<b>CRYPTO \u0412\u042B\u0416\u0418\u041C\u041A\u0410</b>"

    if not total:
        body = "\n\n<i>\u0421\u0435\u0433\u043E\u0434\u043D\u044F \u0442\u0438\u0445\u043E \u2014 \u0441\u0442\u043E\u044F\u0449\u0438\u0445 \u043F\u043E\u0441\u0442\u043E\u0432 \u043D\u0435 \u043D\u0430\u0448\u043B\u043E\u0441\u044C.</i>"
        if verbose and digest.errors:
            body += "\n\n\u26A0\ufe0f " + "\n\u26A0\ufe0f ".join(esc(e) for e in digest.errors)
        return [header + body + footer_link]

    flat: list[Line] = [line for block in digest.blocks for line in block.items]
    flat.sort(key=lambda line: (-line.rank, -line.views, line.ts))

    lines = []
    for line in flat:
        href = escape(line.link, quote=True)
        label = esc(line.summary.rstrip(".").strip() or line.summary)
        lines.append(f'<b>{esc(line.channel_title)}:</b> <a href="{href}">{label}</a>')

    messages: list[str] = []
    current = header
    for text in lines:
        candidate = f"{current}\n{text}"
        if len(candidate) + len(footer_link) <= SAFE:
            current = candidate
        else:
            messages.append(current)
            current = text
    current += footer_link
    if verbose and digest.errors:
        note = "\n\n\u26A0\ufe0f " + "\n\u26A0\ufe0f ".join(esc(e) for e in digest.errors)
        if len(current) + len(note) <= SAFE:
            current += note
        else:
            messages.append(current)
            current = note.strip()
    messages.append(current)
    return messages
