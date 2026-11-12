from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.models import Application, ApplicationActivity, BotMessage, Job, utcnow
from app.services.bot_config import validate_config
from app.telegram.client import TelegramClient, TelegramError
from app.telegram.controller import BotController
from app.telegram.delivery import (
    deliver_one,
    prepare_message,
    queue_notifications,
    recover_delivery,
)
from app.telegram.views import job_card


def incoming(number: int, text: str = "/start", *, user: int = 42, chat_type="private"):
    return {
        "update_id": number,
        "message": {
            "from": {"id": user},
            "chat": {"id": user, "type": chat_type},
            "text": text,
        },
    }


def callback(number: int, data: str, *, user: int = 42):
    return {
        "update_id": number,
        "callback_query": {
            "id": str(number),
            "from": {"id": user},
            "data": data,
            "message": {"message_id": 500, "chat": {"id": 42, "type": "private"}},
        },
    }


@pytest.fixture
def controller():
    return BotController(42, validate_config({"minimum_score": 0, "page_size": 2}))


@pytest.fixture
def job(session):
    item = Job(
        title="Finance Intern",
        company="Example",
        source="Test",
        location="Kraków",
        canonical_url="https://example.com/telegram",
        original_url="https://example.com/telegram",
        job_hash="telegram",
        match_score=80,
    )
    session.add(item)
    session.commit()
    return item


def test_authorization_checks_sender_and_private_chat(session, controller, job):
    controller.handle(session, incoming(1, f"/note {job.id} stolen", user=43))
    controller.handle(session, incoming(2, "/start", chat_type="group"))
    controller.handle(session, callback(3, f"apply:{job.id}", user=43))
    forged = incoming(4, f"/note {job.id} forged")
    forged["message"]["chat"]["id"] = 99
    controller.handle(session, forged)
    assert job.application is None
    assert session.scalar(select(func.count(BotMessage.id))) == 0
    assert controller.state(session, "offset") == 5


def test_replayed_updates_and_repeated_buttons_are_idempotent(session, controller, job):
    controller.handle(session, callback(1, f"apply:{job.id}"))
    events = session.scalar(select(func.count(ApplicationActivity.id)))
    controller.handle(session, callback(1, f"apply:{job.id}"))
    controller.handle(session, callback(2, f"apply:{job.id}"))
    assert job.application.status == "Applied"
    assert session.scalar(select(func.count(ApplicationActivity.id))) == events
    assert session.scalar(select(func.count(BotMessage.id))) == 2
    controller.handle(session, callback(3, f"ignore:{job.id}"))
    controller.handle(session, callback(4, f"ignore:{job.id}"))
    assert job.ignored
    assert (
        session.scalar(
            select(BotMessage).where(BotMessage.dedup_key.endswith(":update:1:0"))
        ).method
        == "editMessageText"
    )


def test_failed_response_queue_rolls_back_action_and_cursor(session, controller, job, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("database write interrupted")

    with monkeypatch.context() as context:
        context.setattr(controller, "queue", fail)
        with pytest.raises(RuntimeError):
            controller.handle(session, callback(1, f"apply:{job.id}"))
        session.rollback()
    session.expire_all()
    assert job.application is None
    assert controller.state(session, "offset", 0) == 0
    controller.handle(session, callback(1, f"apply:{job.id}"))
    assert job.application.status == "Applied"


def test_invalid_input_is_acknowledged_without_partial_changes(session, controller, job):
    controller.handle(session, incoming(1, f"/date {job.id} 2026-99-99"))
    controller.handle(session, callback(2, f"status:{job.id}:-1"))
    assert job.application is None
    assert controller.state(session, "offset") == 3
    assert session.scalar(select(func.count(BotMessage.id))) == 2


def test_notes_dates_cv_and_legacy_status_work_through_commands(session, controller, job):
    controller.handle(session, incoming(1, f"/note {job.id} Rozmowa jutro"))
    controller.handle(session, incoming(2, f"/date {job.id} 2026-12-01"))
    controller.handle(session, incoming(3, f"/cv {job.id} Finance v2"))
    controller.handle(session, callback(4, f"status:{job.id}:2"))
    assert job.application.status == "Applied"
    assert job.application.notes == "Rozmowa jutro"
    assert job.application.next_action_date == date(2026, 12, 1)
    assert job.application.cv_version == "Finance v2"
    controller.handle(session, incoming(5, f"/date {job.id} -"))
    assert job.application.next_action_date is None
    controller.handle(session, incoming(6, f"/note {job.id} -"))
    assert job.application.notes == ""


def test_search_pagination_and_escaping(session, controller, job):
    job.title = "<Finance Intern & Research>"
    for number in range(5):
        session.add(
            Job(
                title=f"Finance Intern {number}",
                company="Example",
                source="Test",
                original_url=f"https://example.com/{number}",
                canonical_url=f"https://example.com/{number}",
                job_hash=str(number),
            )
        )
    session.commit()
    controller.handle(session, incoming(1, "/find Finance"))
    controller.handle(session, callback(2, "list:find:2"))
    replies = session.scalars(
        select(BotMessage).where(BotMessage.dedup_key.contains(":update:2:"))
    ).all()
    assert "Strona 2/3" in replies[0].payload["text"]
    assert "Finance" in replies[0].payload["text"]
    assert len(replies) == 4
    assert "&lt;Finance Intern &amp; Research&gt;" in job_card(job)["text"]
    assert "<Finance" not in job_card(job)["text"]


def test_notifications_survive_restart_without_requeuing_old_jobs(session, controller, job):
    controller.set_state(session, "started_at", (utcnow() - timedelta(days=1)).isoformat())
    queue_notifications(session, controller, utcnow())
    assert session.scalar(select(func.count(BotMessage.id))) == 1
    message = session.scalar(select(BotMessage))
    message.status = "sent"
    session.commit()
    restarted = BotController(42, controller.config)
    queue_notifications(session, restarted, utcnow())
    assert session.scalar(select(func.count(BotMessage.id))) == 1
    # Existing database entries are available in lists without a first-start notification flood.
    old = Job(
        title="Old Intern",
        source="Test",
        company="Old",
        original_url="https://example.com/old",
        canonical_url="https://example.com/old",
        job_hash="old",
        date_discovered=utcnow() - timedelta(days=30),
    )
    session.add(old)
    session.commit()
    queue_notifications(session, restarted, utcnow())
    assert session.scalar(select(func.count(BotMessage.id))) == 1


def test_pause_and_changed_offer_cancel_stale_delivery(session, controller, job):
    controller.set_state(session, "started_at", (utcnow() - timedelta(days=1)).isoformat())
    queue_notifications(session, controller, utcnow())
    controller.set_state(session, "paused", True)
    session.commit()
    assert prepare_message(session, controller) is None
    controller.set_state(session, "paused", False)
    job.ignored = True
    session.commit()
    assert prepare_message(session, controller) is None
    assert session.scalar(select(BotMessage)).status == "cancelled"


def test_reminders_use_warsaw_day_and_skip_closed_processes(session, controller, job):
    now = datetime(2026, 11, 11, 9, 30)
    controller.set_state(session, "started_at", now.isoformat())
    session.add(Application(job=job, status="Applied", next_action_date=date(2026, 11, 11)))
    session.commit()
    queue_notifications(session, controller, now)
    queue_notifications(session, controller, now)
    assert session.scalar(select(func.count(BotMessage.id))) == 1
    message = session.scalar(select(BotMessage))
    assert ":reminder:" in message.dedup_key
    assert "2026-11-11" in message.dedup_key
    job.application.status = "Rejected"
    session.commit()
    assert prepare_message(session, controller) is None
    assert message.status == "cancelled"


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (None, "sent"),
        (httpx.ConnectError("offline"), "pending"),
        (httpx.ReadTimeout("unknown result"), "uncertain"),
        (TelegramError(429, "rate limit", 45), "pending"),
        (TelegramError(403, "blocked"), "failed"),
        (TelegramError(502, "upstream unavailable"), "uncertain"),
    ],
)
def test_delivery_records_result_and_safe_retry_policy(session, controller, error, status):
    controller.queue(session, "test", {"text": "Wiadomość"})
    session.commit()
    sessions = sessionmaker(bind=session.get_bind(), expire_on_commit=False)

    class Client:
        async def call(self, method, **payload):
            if error:
                raise error
            return {"message_id": 10}

    assert asyncio.run(deliver_one(Client(), sessions, controller))
    session.expire_all()
    message = session.scalar(select(BotMessage))
    assert message.status == status
    assert message.attempts == 1
    if status == "sent":
        assert message.sent_at is not None
    if status in {"uncertain", "failed"}:
        assert not asyncio.run(deliver_one(Client(), sessions, controller))
    if isinstance(error, TelegramError) and error.code == 429:
        assert message.available_at > utcnow() + timedelta(seconds=40)
        assert controller.state(session, "delivery_after")


def test_restart_marks_in_flight_message_uncertain(session, controller):
    controller.queue(session, "test", {"text": "Wiadomość"})
    session.commit()
    message = prepare_message(session, controller)
    assert message.status == "sending"
    recover_delivery(session, controller)
    session.refresh(message)
    assert message.status == "uncertain"
    assert prepare_message(session, controller) is None
    controller.handle(session, incoming(1, f"/delivery {message.id}"))
    session.refresh(message)
    assert message.status == "pending"


def test_client_posts_json_and_exposes_retry_after():
    async def exercise():
        def handler(request):
            assert request.url.path == "/botfake/getUpdates"
            assert request.method == "POST"
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "description": "Too many requests",
                    "parameters": {"retry_after": 33},
                },
            )

        client = TelegramClient("fake")
        await client.http.aclose()
        client.http = httpx.AsyncClient(
            base_url="https://api.telegram.org/botfake/",
            transport=httpx.MockTransport(handler),
        )
        try:
            with pytest.raises(TelegramError) as captured:
                await client.call("getUpdates", timeout=25)
            assert captured.value.retry_after == 33
            assert "fake" not in str(captured.value)
        finally:
            await client.close()

    asyncio.run(exercise())
