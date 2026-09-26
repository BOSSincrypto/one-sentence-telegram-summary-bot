from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import cronsync, db, llm, runner

MSK = ZoneInfo("Europe/Moscow")


def at(hour: int, minute: int = 0, day: int = 26) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=MSK)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("09:00", (9, 0)), ("7:05", (7, 5)), ("23:59", (23, 59)), ("нет", (9, 0)), ("", (9, 0))],
)
def test_parse_time(raw, expected):
    assert runner.parse_time(raw) == expected


def test_not_due_before_the_scheduled_moment():
    db.put("digest_time", "09:00")
    assert runner.is_due(at(8, 59)) is False


def test_due_at_and_after_the_scheduled_moment():
    db.put("digest_time", "09:00")
    assert runner.is_due(at(9, 0)) is True
    assert runner.is_due(at(14, 30)) is True


def test_not_due_twice_on_the_same_day():
    db.put("digest_time", "09:00")
    db.put(runner.LAST_DAY_KEY, "2026-07-26")

    assert runner.is_due(at(9, 30)) is False
    assert runner.is_due(at(9, 30, day=27)) is True  # a new day re-arms it


def test_next_run_is_today_when_still_ahead():
    db.put("digest_time", "09:00")
    assert runner.next_run_at(at(7, 0)) == at(9, 0)


def test_next_run_rolls_to_tomorrow_once_today_is_done():
    db.put("digest_time", "09:00")
    db.put(runner.LAST_DAY_KEY, "2026-07-26")
    assert runner.next_run_at(at(10, 0)) == at(9, 0, day=27)


def test_next_run_reports_now_when_overdue():
    db.put("digest_time", "09:00")
    now = at(11, 0)
    assert runner.next_run_at(now) == now


def test_timezone_change_moves_the_schedule():
    db.put("digest_time", "09:00")
    db.put("tz", "Asia/Almaty")
    # 09:00 Almaty is 06:00 Moscow, so a 07:00 Moscow instant is already past it.
    assert runner.is_due(at(7, 0)) is True


def test_month_boundary_follows_the_configured_timezone():
    db.put("tz", "Pacific/Kiritimati")  # UTC+14
    assert db.local_date().isoformat() >= datetime.now(ZoneInfo("UTC")).date().isoformat()


# --------------------------------------------------------------------------- #
# external alarm
# --------------------------------------------------------------------------- #


def test_cron_schedule_adds_a_retry_inside_the_same_hour():
    schedule = cronsync.schedule_for(9, 0, "Europe/Moscow")

    assert schedule["hours"] == [9]
    assert schedule["minutes"] == [0, 15]
    assert schedule["mdays"] == schedule["months"] == schedule["wdays"] == [-1]
    assert schedule["timezone"] == "Europe/Moscow"


def test_cron_schedule_skips_a_retry_that_would_cross_the_hour():
    schedule = cronsync.schedule_for(9, 50, "UTC")
    assert schedule["minutes"] == [50]


def test_cron_description_is_a_readable_expression():
    assert cronsync.describe(9, 0, "Europe/Moscow") == "0,15 9 * * *  (Europe/Moscow)"


# --------------------------------------------------------------------------- #
# budget guard
# --------------------------------------------------------------------------- #


def test_budget_guard_allows_spending_under_the_cap():
    db.put("budget_usd", 5.0)
    db.add_usage(db.local_date().isoformat(), "m", 100, 10, 1.5)
    llm.OpenRouter._check_budget()  # must not raise


def test_budget_guard_stops_spending_at_the_cap():
    db.put("budget_usd", 1.0)
    db.add_usage(db.local_date().isoformat(), "m", 100, 10, 1.25)

    with pytest.raises(llm.BudgetExceeded):
        llm.OpenRouter._check_budget()


def test_no_cap_means_no_guard():
    db.put("budget_usd", 0)
    db.add_usage(db.local_date().isoformat(), "m", 100, 10, 999.0)
    llm.OpenRouter._check_budget()


def test_prune_drops_stale_rows_but_keeps_recent_ones():
    now = int(datetime.now(ZoneInfo("UTC")).timestamp())
    db.mark_sent(1, [(1, 1)])
    db.db().execute("UPDATE sent SET ts=?", (now - 10 * 86400,))
    db.mark_sent(1, [(1, 2)])
    db.store_summaries([("old", "s", False)])
    db.db().execute("UPDATE summary SET ts=? WHERE k='old'", (now - 30 * 86400,))

    db.prune(now)

    remaining = {int(r["post_id"]) for r in db.db().execute("SELECT post_id FROM sent")}
    assert remaining == {2}
    assert db.cached_summaries(["old"]) == {}


def test_summary_cache_round_trip():
    db.store_summaries([("k1", "Кратко.", True)])
    assert db.cached_summaries(["k1", "missing"]) == {"k1": ("Кратко.", True)}


def test_already_sent_reports_only_known_pairs():
    db.mark_sent(7, [(1, 10), (1, 11)])
    assert db.already_sent(7, [(1, 10), (1, 99)]) == {(1, 10)}
    assert db.already_sent(8, [(1, 10)]) == set()


def test_settings_fall_back_to_defaults_then_persist():
    assert db.get("max_posts") == db.DEFAULTS["max_posts"]
    db.put("max_posts", 7)
    db.reset_cache()
    assert db.get("max_posts") == 7


def test_group_channel_toggle_is_idempotent_in_both_directions():
    channel_id = db.add_channel("chan")
    group_id = db.add_group("Группа")

    assert db.toggle_group_channel(group_id, channel_id) is True
    assert db.group_channel_ids(group_id) == {channel_id}
    assert db.toggle_group_channel(group_id, channel_id) is False
    assert db.group_channel_ids(group_id) == set()


def test_deleting_a_channel_detaches_it_from_groups():
    channel_id = db.add_channel("chan")
    group_id = db.add_group("Группа")
    db.toggle_group_channel(group_id, channel_id)

    db.delete_channel(channel_id)

    assert db.group_channel_ids(group_id) == set()


def test_adding_a_known_channel_updates_the_title_without_duplicating():
    first = db.add_channel("chan", "Старое имя")
    second = db.add_channel("chan", "Новое имя")

    assert first == second
    assert len(db.channels()) == 1
    assert db.channel(first)["title"] == "Новое имя"


def test_midnight_schedule_rolls_a_full_day_forward():
    db.put("digest_time", "00:00")
    db.put(runner.LAST_DAY_KEY, "2026-07-26")
    assert runner.next_run_at(at(1, 0)) - at(0, 0) == timedelta(days=1)


@pytest.mark.asyncio
async def test_run_all_keeps_due_when_a_group_crashes(monkeypatch):
    group_id = db.add_group("Группа")
    db.update_group(group_id, chat_id=1)
    db.put("digest_time", "00:00")
    db.put(runner.LAST_DAY_KEY, "2000-01-01")

    async def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(runner, "run_group", boom)
    await runner.run_all(bot=None, session=None, client=None)

    assert runner.is_due() is True
