"""Keeps the external wake-up alarm in sync with the schedule set in the bot.

The Fly machine sleeps between digests, and Fly does not wake machines on a
schedule — something outside must call ``/tick``. cron-job.org does that with
minute precision for free, and it has a REST API, so the bot can reprogram the
alarm itself whenever the digest time changes. Without an API key the bot falls
back to printing the exact URL and schedule for the owner to paste in by hand.
"""

from __future__ import annotations

from aiohttp import ClientSession, ClientTimeout

from . import db

API = "https://api.cron-job.org"
_TIMEOUT = ClientTimeout(total=20)
TITLE = "Telegram Digest Bot — tick"

# Retrying 15 minutes later costs one extra wake-up and covers a transient
# failure to reach the machine. It is skipped when it would spill into the
# next hour, where it could not be expressed without date-boundary bugs.
RETRY_AFTER_MIN = 15


class CronError(Exception):
    pass


def schedule_for(hour: int, minute: int, timezone: str) -> dict:
    minutes = [minute]
    if minute + RETRY_AFTER_MIN < 60:
        minutes.append(minute + RETRY_AFTER_MIN)
    return {
        "timezone": timezone,
        "hours": [hour],
        "minutes": minutes,
        "mdays": [-1],
        "months": [-1],
        "wdays": [-1],
    }


def describe(hour: int, minute: int, timezone: str) -> str:
    schedule = schedule_for(hour, minute, timezone)
    minutes = ",".join(str(m) for m in schedule["minutes"])
    return f"{minutes} {hour} * * *  ({timezone})"


async def _request(
    session: ClientSession, key: str, method: str, path: str, payload: dict | None = None
) -> dict:
    headers = {"Authorization": f"Bearer {key}"}
    try:
        async with session.request(
            method, f"{API}{path}", json=payload, headers=headers, timeout=_TIMEOUT
        ) as resp:
            text = await resp.text()
            if resp.status == 401:
                raise CronError("ключ cron-job.org отклонён (401)")
            if resp.status == 404:
                raise CronError("задача не найдена (404)")
            if resp.status >= 400:
                raise CronError(f"HTTP {resp.status}: {text[:140]}")
            return await resp.json() if text else {}
    except CronError:
        raise
    except Exception as exc:
        raise CronError(f"сеть недоступна: {type(exc).__name__}") from exc


async def sync(session: ClientSession, key: str, tick_url: str, hour: int, minute: int) -> str:
    """Creates or updates the alarm. Returns a short status line for the UI."""
    if not key:
        raise CronError("ключ cron-job.org не задан")

    job = {
        "url": tick_url,
        "enabled": True,
        "saveResponses": False,
        "title": TITLE,
        "requestTimeout": 30,
        "schedule": schedule_for(hour, minute, str(db.get("tz"))),
    }

    job_id = int(db.get("cron_job_id") or 0)
    if job_id:
        try:
            await _request(session, key, "PATCH", f"/jobs/{job_id}", {"job": job})
            return f"обновлена задача #{job_id}"
        except CronError as exc:
            if "404" not in str(exc):
                raise
            db.put("cron_job_id", 0)

    created = await _request(session, key, "PUT", "/jobs", {"job": job})
    new_id = int(created.get("jobId") or 0)
    if not new_id:
        raise CronError("cron-job.org не вернул jobId")
    db.put("cron_job_id", new_id)
    return f"создана задача #{new_id}"


async def status(session: ClientSession, key: str) -> str:
    job_id = int(db.get("cron_job_id") or 0)
    if not key or not job_id:
        return ""
    data = await _request(session, key, "GET", f"/jobs/{job_id}")
    job = data.get("jobDetails") or data.get("job") or {}
    if not job:
        return ""
    last = job.get("lastStatus")
    return "последний вызов: успешно" if last == 1 else f"последний вызов: код {last}"
