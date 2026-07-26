"""Fixtures reproducing the real t.me/s/ markup verified against live channels.

Covers the shapes that actually trip a parser: a reply block (whose inner text
uses ``js-message_reply_text``, not ``js-message_text``), a media-only post, a
service message, ``<br/>`` line breaks and abbreviated view counts.
"""

from __future__ import annotations

HEAD = """<!DOCTYPE html><html><head>
<meta property="og:title" content="Тестовый канал">
</head><body><main><section class="tgme_channel_history js-message_history">
<div class="tgme_widget_message_centered js-messages_more_wrap">
  <a href="/s/testchan?before=100" class="tme_messages_more js-messages_more" data-before="100"></a>
</div>
"""

TAIL = "</section></main></body></html>"

PLAIN = """
<div class="tgme_widget_message_wrap js-widget_message_wrap">
<div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="testchan/101">
  <div class="tgme_widget_message_bubble">
    <div class="tgme_widget_message_text js-message_text" dir="auto">
      <b>Заголовок новости</b><br/><br/>Подробности события с числом 42 и ссылкой
      <a href="https://example.com">тут</a>.
    </div>
    <div class="tgme_widget_message_footer">
      <div class="tgme_widget_message_info">
        <span class="tgme_widget_message_views">4.5M</span>
        <a class="tgme_widget_message_date" href="https://t.me/testchan/101">
          <time datetime="2026-07-26T09:14:00+00:00" class="time">09:14</time></a>
      </div>
    </div>
  </div>
</div></div>
"""

WITH_REPLY = """
<div class="tgme_widget_message_wrap js-widget_message_wrap">
<div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="testchan/102">
  <div class="tgme_widget_message_bubble">
    <a class="tgme_widget_message_reply" href="https://t.me/testchan/99">
      <div class="tgme_widget_message_metatext js-message_reply_text" dir="auto">ЦИТИРУЕМЫЙ ТЕКСТ</div>
    </a>
    <div class="tgme_widget_message_text js-message_text" dir="auto">Ответ на предыдущий пост.</div>
    <div class="tgme_widget_message_footer"><div class="tgme_widget_message_info">
      <span class="tgme_widget_message_views">82K</span>
      <a class="tgme_widget_message_date" href="https://t.me/testchan/102">
        <time datetime="2026-07-26T10:30:00+00:00" class="time">10:30</time></a>
    </div></div>
  </div>
</div></div>
"""

MEDIA_ONLY = """
<div class="tgme_widget_message_wrap js-widget_message_wrap">
<div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="testchan/103">
  <div class="tgme_widget_message_bubble">
    <a class="tgme_widget_message_photo_wrap" href="https://t.me/testchan/103"></a>
    <div class="tgme_widget_message_footer"><div class="tgme_widget_message_info">
      <span class="tgme_widget_message_views">1 200</span>
      <a class="tgme_widget_message_date" href="https://t.me/testchan/103">
        <time datetime="2026-07-26T11:00:00+00:00" class="time">11:00</time></a>
    </div></div>
  </div>
</div></div>
"""

SERVICE = """
<div class="tgme_widget_message_wrap js-widget_message_wrap">
<div class="tgme_widget_message service_message js-widget_message" data-post="testchan/104">
  <div class="tgme_widget_message_bubble">
    <div class="tgme_widget_message_text js-message_text">Канал закреплён</div>
    <a class="tgme_widget_message_date" href="https://t.me/testchan/104">
      <time datetime="2026-07-26T11:30:00+00:00" class="time">11:30</time></a>
  </div>
</div></div>
"""

PREVIEW_ONLY = """
<div class="tgme_widget_message_wrap js-widget_message_wrap">
<div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="testchan/105">
  <div class="tgme_widget_message_bubble">
    <a class="tgme_widget_message_link_preview" href="https://example.com">
      <div class="link_preview_title">Заголовок из превью</div>
      <div class="link_preview_description">Описание из превью ссылки.</div>
    </a>
    <div class="tgme_widget_message_footer"><div class="tgme_widget_message_info">
      <span class="tgme_widget_message_views">7</span>
      <a class="tgme_widget_message_date" href="https://t.me/testchan/105">
        <time datetime="2026-07-26T12:00:00+00:00" class="time">12:00</time></a>
    </div></div>
  </div>
</div></div>
"""

LAST_PAGE_HEAD = HEAD.replace(
    '<a href="/s/testchan?before=100" class="tme_messages_more js-messages_more" data-before="100"></a>',
    "",
)


def page(*blocks: str, last: bool = False) -> str:
    return (LAST_PAGE_HEAD if last else HEAD) + "".join(blocks) + TAIL


FULL = page(PLAIN, WITH_REPLY, MEDIA_ONLY, SERVICE, PREVIEW_ONLY)
