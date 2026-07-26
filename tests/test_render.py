from __future__ import annotations

import re

from app import db, render
from app.digest import Block, Digest, Line

WINDOW_END = 1_785_000_000  # fixed point so headers are deterministic


def line(pid: int, summary: str, *, views: int = 0, also: list[str] | None = None) -> Line:
    return Line(
        channel_id=1,
        channel="chan",
        channel_title="Канал",
        post_id=pid,
        ts=WINDOW_END - 3600,
        views=views,
        text=summary,
        full=True,
        summary=summary,
        also_in=also or [],
    )


def digest_with(*blocks: Block, **kwargs) -> Digest:
    return Digest(
        group_id=1,
        group_name="Новости",
        group_emoji="🗞",
        blocks=list(blocks),
        window_start=WINDOW_END - 86400,
        window_end=WINDOW_END,
        **kwargs,
    )


def block(*lines: Line, extra: list[Line] | None = None, hidden: int = 0) -> Block:
    return Block(
        channel_id=1,
        username="chan",
        title="Канал",
        items=list(lines),
        extra=extra or [],
        hidden=hidden,
    )


def test_each_summary_links_to_its_own_post():
    out = render.render(digest_with(block(line(101, "Первое."), line(102, "Второе."))))
    text = "\n".join(out)

    assert '<a href="https://t.me/chan/101">1</a> · Первое.' in text
    assert '<a href="https://t.me/chan/102">2</a> · Второе.' in text


def test_header_reports_channels_and_posts():
    out = render.render(digest_with(block(line(1, "Раз."), line(2, "Два."))))

    assert out[0].startswith("📰 <b>🗞 Новости</b>")
    assert "1 канал" in out[0]
    assert "2 поста" in out[0]


def test_html_in_a_summary_is_escaped():
    out = render.render(digest_with(block(line(1, "Курс <b>вырос</b> & упал"))))
    text = "\n".join(out)

    assert "&lt;b&gt;вырос&lt;/b&gt; &amp; упал" in text
    # Only our own markup survives as real tags.
    assert "<b>вырос</b>" not in text


def test_long_channels_fold_into_an_expandable_quote():
    db.put("collapse_at", 3)
    lines = [line(i, f"Новость {i}.") for i in range(1, 6)]
    out = render.render(digest_with(block(*lines)))
    text = "\n".join(out)

    assert "<blockquote expandable>" in text
    assert text.count("<blockquote expandable>") == text.count("</blockquote>")


def test_short_channels_are_not_folded():
    db.put("collapse_at", 6)
    out = render.render(digest_with(block(line(1, "Одна."), line(2, "Две."))))

    assert "<blockquote" not in "\n".join(out)


def test_overflow_topics_are_rendered_as_links():
    extra = [line(9, "Тема одна"), line(10, "Тема два")]
    out = render.render(digest_with(block(line(1, "Главное."), extra=extra)))
    text = "\n".join(out)

    assert "Также писали про:" in text
    assert '<a href="https://t.me/chan/9">Тема одна</a>' in text


def test_hidden_count_is_shown():
    out = render.render(digest_with(block(line(1, "Главное."), hidden=7)))
    assert "и ещё 7 постов" in "\n".join(out)


def test_views_and_cross_posting_appear_only_when_asked():
    db.put("show_views", True)
    out = render.render(digest_with(block(line(1, "Событие.", views=4_500_000, also=["Другой"]))))
    text = "\n".join(out)

    assert "👁 4.5M" in text
    assert "также: Другой" in text


def test_empty_digest_says_so_instead_of_sending_a_bare_header():
    out = render.render(digest_with())
    assert len(out) == 1
    assert "нечего показать" in out[0]


def test_every_message_stays_under_the_telegram_limit():
    db.put("collapse_at", 1000)  # no folding, so packing must do the work
    lines = [line(i, f"Довольно длинное предложение под номером {i}. " * 3) for i in range(1, 120)]
    out = render.render(digest_with(block(*lines)))

    assert len(out) > 1
    assert all(len(message) <= render.LIMIT for message in out)


def test_split_parts_each_carry_the_channel_heading():
    db.put("collapse_at", 1000)
    lines = [line(i, f"Длинное предложение номер {i}. " * 4) for i in range(1, 200)]
    out = render.render(digest_with(block(*lines)))

    assert len(out) > 2
    for message in out[1:]:
        assert "📣 <b>Канал</b>" in message


def test_a_folded_block_that_overflows_reopens_its_quote_in_each_part():
    db.put("collapse_at", 1)
    lines = [line(i, f"Длинное предложение номер {i}. " * 4) for i in range(1, 200)]
    out = render.render(digest_with(block(*lines)))

    for message in out:
        assert message.count("<blockquote expandable>") == message.count("</blockquote>")


def test_verbose_footer_reports_cost_and_model_only_in_preview():
    payload = {"cost": 0.0123, "models": ["fake/model"], "deduped": 2}
    quiet = render.render(digest_with(block(line(1, "Раз.")), **payload))
    loud = render.render(digest_with(block(line(1, "Раз.")), **payload), verbose=True)

    assert "$0.0123" not in "\n".join(quiet)
    assert "fake/model" not in "\n".join(quiet)
    assert "$0.0123" in "\n".join(loud)
    assert "2 дубля свёрнуто" in "\n".join(quiet)  # counts are useful to everyone


def test_plural_forms_follow_russian_grammar():
    assert render.plural(1, "пост", "поста", "постов") == "пост"
    assert render.plural(3, "пост", "поста", "постов") == "поста"
    assert render.plural(11, "пост", "поста", "постов") == "постов"
    assert render.plural(22, "пост", "поста", "постов") == "поста"


def test_view_counts_are_humanised():
    assert render.human_views(999) == "999"
    assert render.human_views(82_000) == "82K"
    assert render.human_views(4_530_000) == "4.5M"


def test_no_message_contains_an_unclosed_tag():
    db.put("collapse_at", 2)
    lines = [line(i, f"Новость номер {i}.") for i in range(1, 10)]
    out = render.render(digest_with(block(*lines)))

    for message in out:
        for tag in ("b", "i", "a", "blockquote"):
            opens = len(re.findall(rf"<{tag}(?:\s[^>]*)?>", message))
            closes = len(re.findall(rf"</{tag}>", message))
            assert opens == closes, f"{tag} unbalanced in: {message[:120]}"
