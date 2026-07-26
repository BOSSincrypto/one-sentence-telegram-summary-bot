from __future__ import annotations

import pytest

from app import tme
from tests import fixtures


def test_normalize_username_accepts_every_common_form():
    for raw in ("@MeduzaLive", "meduzalive", "t.me/meduzalive", "https://t.me/s/meduzalive?x=1"):
        assert tme.normalize_username(raw) == "meduzalive"


@pytest.mark.parametrize("raw", ["", "@a", "not a username!", "https://example.com/x"])
def test_normalize_username_rejects_junk(raw):
    with pytest.raises(tme.ChannelError):
        tme.normalize_username(raw)


def test_parse_page_extracts_posts_and_cursor():
    title, posts, before = tme.parse_page(fixtures.FULL, "testchan")

    assert title == "Тестовый канал"
    assert before == 100
    # The service message is dropped; everything else survives.
    assert [p.id for p in posts] == [101, 102, 103, 105]


def test_parse_page_keeps_line_breaks_and_ignores_quoted_text():
    _, posts, _ = tme.parse_page(fixtures.FULL, "testchan")
    by_id = {p.id: p for p in posts}

    assert by_id[101].text.startswith("Заголовок новости\n")
    assert "42" in by_id[101].text
    assert by_id[101].views == 4_500_000

    # A reply block must not leak the quoted message into the post body.
    assert "ЦИТИРУЕМЫЙ" not in by_id[102].text
    assert by_id[102].text == "Ответ на предыдущий пост."
    assert by_id[102].views == 82_000


def test_media_only_post_has_no_text_but_is_flagged():
    _, posts, _ = tme.parse_page(fixtures.FULL, "testchan")
    media = next(p for p in posts if p.id == 103)

    assert media.text == ""
    assert media.has_media is True
    assert media.views == 1200


def test_link_preview_is_used_when_the_post_has_no_body():
    _, posts, _ = tme.parse_page(fixtures.FULL, "testchan")
    preview = next(p for p in posts if p.id == 105)

    assert "Заголовок из превью" in preview.text
    assert "Описание из превью ссылки." in preview.text


def test_last_page_reports_no_cursor():
    _, _, before = tme.parse_page(fixtures.page(fixtures.PLAIN, last=True), "testchan")
    assert before is None


def test_permalink_points_at_the_original_post():
    _, posts, _ = tme.parse_page(fixtures.FULL, "testchan")
    assert posts[0].link == "https://t.me/testchan/101"


class _FakeResponse:
    def __init__(self, body: str):
        self.status = 200
        self.headers: dict[str, str] = {}
        self._body = body

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Serves a two-page history so pagination can be exercised offline."""

    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.requested: list[str] = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        return _FakeResponse(self.pages[url])


async def test_fetch_posts_pages_backwards_until_the_window_closes():
    older = fixtures.page(
        fixtures.PLAIN.replace("testchan/101", "testchan/91").replace(
            "2026-07-26T09:14:00+00:00", "2026-07-20T09:00:00+00:00"
        ),
        last=True,
    )
    session = _FakeSession(
        {
            "https://t.me/s/testchan": fixtures.FULL,
            "https://t.me/s/testchan?before=100": older,
        }
    )

    since = 1_753_000_000  # 2025 — well before every fixture timestamp
    title, posts = await tme.fetch_posts(session, "testchan", since, max_pages=5)

    assert title == "Тестовый канал"
    assert len(session.requested) == 2
    assert {p.id for p in posts} == {91, 101, 102, 103, 105}
    assert [p.ts for p in posts] == sorted(p.ts for p in posts)


async def test_fetch_posts_stops_after_one_request_for_a_quiet_channel():
    session = _FakeSession({"https://t.me/s/testchan": fixtures.FULL})

    # A window starting after every fixture post: page one already reaches past it.
    _, posts = await tme.fetch_posts(session, "testchan", 1_800_000_000, max_pages=8)

    assert posts == []
    assert session.requested == ["https://t.me/s/testchan"]


async def test_redirect_is_reported_as_a_missing_or_private_channel():
    class Redirect(_FakeResponse):
        def __init__(self):
            super().__init__("")
            self.status = 302

    class RedirectSession:
        def get(self, url, **kwargs):
            return Redirect()

    with pytest.raises(tme.ChannelError, match="не найден"):
        await tme.fetch_posts(RedirectSession(), "gone", 0)
