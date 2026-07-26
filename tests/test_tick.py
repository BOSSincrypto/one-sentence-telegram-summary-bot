"""The /tick endpoint is the only thing that wakes the sleeping Machine."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web

from app import db, main, runner

KEY = "correct-tick-key"


class Recorder:
    def __init__(self, *, slow: bool = False, boom: bool = False):
        self.runs = 0
        self.slow = slow
        self.boom = boom

    async def __call__(self, app):
        self.runs += 1
        if self.boom:
            raise RuntimeError("digest exploded")
        if self.slow:
            await asyncio.sleep(0.4)


@pytest.fixture
def client_app(monkeypatch):
    recorder = Recorder()

    async def run_due(app):
        await recorder(app)

    monkeypatch.setattr(main, "_run_due", run_due)

    app = web.Application()
    app[main.CONFIG] = type("Cfg", (), {"tick_key": KEY, "owner_ids": frozenset()})()
    app[main.RUNTIME] = {}
    app.router.add_route("*", "/tick", main.handle_tick)
    return app, recorder


async def request(app, query: str) -> tuple[int, dict]:
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get(f"/tick{query}")
        body = await resp.json() if resp.content_type == "application/json" else {}
        return resp.status, body


async def test_wrong_key_is_rejected_and_runs_nothing(client_app):
    app, recorder = client_app
    db.put("digest_time", "00:00")

    status, _ = await request(app, "?key=guessed")

    assert status == 403
    assert recorder.runs == 0


async def test_missing_key_is_rejected(client_app):
    app, recorder = client_app

    status, _ = await request(app, "")

    assert status == 403
    assert recorder.runs == 0


async def test_tick_before_the_scheduled_time_goes_straight_back_to_sleep(client_app):
    app, recorder = client_app
    db.put("digest_time", "23:59")
    db.put(runner.LAST_DAY_KEY, db.local_date().isoformat())

    status, body = await request(app, f"?key={KEY}")

    assert status == 200
    assert body["status"] == "idle"
    assert "next" in body
    assert recorder.runs == 0  # the wake-up cost nothing


async def test_due_tick_runs_the_digest_and_reports_done(client_app):
    app, recorder = client_app
    db.put("digest_time", "00:00")

    status, body = await request(app, f"?key={KEY}")

    assert status == 200
    assert body["status"] == "done"
    assert recorder.runs == 1


async def test_force_runs_even_when_not_due(client_app):
    app, recorder = client_app
    db.put("digest_time", "23:59")
    db.put(runner.LAST_DAY_KEY, db.local_date().isoformat())

    status, body = await request(app, f"?key={KEY}&force=1")

    assert status == 200
    assert body["status"] == "done"
    assert recorder.runs == 1


async def test_a_second_tick_does_not_start_a_parallel_run(client_app, monkeypatch):
    app, recorder = client_app
    recorder.slow = True
    monkeypatch.setattr(main, "TICK_SYNC_TIMEOUT", 0.05)
    db.put("digest_time", "00:00")

    first_status, first_body = await request(app, f"?key={KEY}")
    second_status, second_body = await request(app, f"?key={KEY}")

    assert (first_status, first_body["status"]) == (202, "accepted")
    assert (second_status, second_body["status"]) == (200, "running")
    assert recorder.runs == 1

    await app[main.RUNTIME]["tick_task"]


async def test_slow_run_detaches_instead_of_holding_the_caller(client_app, monkeypatch):
    app, recorder = client_app
    recorder.slow = True
    monkeypatch.setattr(main, "TICK_SYNC_TIMEOUT", 0.05)
    db.put("digest_time", "00:00")

    status, body = await request(app, f"?key={KEY}")

    assert status == 202
    assert body["status"] == "accepted"
    await app[main.RUNTIME]["tick_task"]  # and it really does finish afterwards
    assert recorder.runs == 1


async def test_a_failing_run_is_logged_not_swallowed(client_app, monkeypatch, caplog):
    app, recorder = client_app
    recorder.boom = True
    db.put("digest_time", "00:00")

    with caplog.at_level("ERROR"):
        status, _ = await request(app, f"?key={KEY}")
        await asyncio.sleep(0)

    # The caller sees a 500, and the failure reaches the log either way.
    assert status == 500
    assert recorder.runs == 1
