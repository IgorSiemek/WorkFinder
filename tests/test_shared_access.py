from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.models import Application, BotMessage, Job, utcnow
from app.services.bot_config import validate_config
from app.telegram import runtime
from app.telegram.controller import BotController
from app.telegram.delivery import prepare_message, queue_notifications


def incoming(number, user, text):
    return {
        "update_id": number,
        "message": {
            "from": {"id": user},
            "chat": {"id": user, "type": "private"},
            "text": text,
        },
    }


@pytest.fixture
def service(session, monkeypatch):
    monkeypatch.setattr(
        runtime,
        "SessionLocal",
        sessionmaker(
            bind=session.get_bind(),
            expire_on_commit=False,
        ),
    )
    owner = BotController(
        42,
        validate_config({"search_times": [], "minimum_score": 0}),
        bot_username="WorkFinderTestBot",
    )
    owner.set_state(session, "started_at", (utcnow() - timedelta(days=1)).isoformat())
    session.commit()
    return runtime.BotRuntime(None, owner)


def invite(session, service):
    payload = service.controller.create_invite(session)
    session.commit()
    url = payload["reply_markup"]["inline_keyboard"][0][0]["url"]
    return parse_qs(urlparse(url).query)["start"][0]


def join(session, service):
    token = invite(session, service)
    assert service.handle_update(incoming(1, 43, "/start " + token))
    session.expire_all()
    return service.controller.for_user(43)


def test_invite_is_one_use_and_persists_across_runtime_restart(session, service):
    token = invite(session, service)
    state = service.controller.state(session, "invite")
    assert token.removeprefix("join_") not in str(state)
    assert service.handle_update(incoming(1, 43, "/start " + token))
    assert not service.handle_update(incoming(2, 44, "/start " + token))
    session.expire_all()
    assert service.controller.allowed_user_ids(session) == [42, 43]
    assert service.controller.state(session, "invite") is None
    messages = session.scalars(select(BotMessage)).all()
    assert {message.payload["chat_id"] for message in messages} == {42, 43}
    assert not any(message.payload["chat_id"] == 44 for message in messages)
    restarted = runtime.BotRuntime(None, BotController(42, service.controller.config))
    assert restarted.handle_update(incoming(3, 43, "/saved"))
    session.expire_all()
    assert service.controller.state(session, "offset") == 4


@pytest.mark.parametrize("case", ["expired", "wrong_token", "group", "forged_chat", "superseded"])
def test_invalid_invitation_cannot_grant_access(session, service, case):
    token = invite(session, service)
    update = incoming(1, 43, "/start " + token)
    if case == "expired":
        state = dict(service.controller.state(session, "invite"))
        state["expires_at"] = (utcnow() - timedelta(seconds=1)).isoformat()
        service.controller.set_state(session, "invite", state)
        session.commit()
    elif case == "wrong_token":
        update["message"]["text"] += "wrong"
    elif case == "group":
        update["message"]["chat"]["type"] = "group"
    elif case == "forged_chat":
        update["message"]["chat"]["id"] = 44
    else:
        invite(session, service)
    assert not service.handle_update(update)
    session.expire_all()
    assert service.controller.allowed_user_ids(session) == [42]
    assert session.scalar(select(func.count(BotMessage.id))) == 0


def test_member_uses_same_application_and_gets_responses_in_own_chat(session, service):
    join(session, service)
    job = Job(
        title="Finance Intern",
        company="Example",
        source="Test",
        original_url="https://example.com/shared",
        canonical_url="https://example.com/shared",
        job_hash="shared",
    )
    session.add(job)
    session.commit()
    assert service.handle_update(incoming(2, 43, f"/note {job.id} Call recruiter"))
    assert service.handle_update(incoming(3, 42, f"/job {job.id}"))
    session.expire_all()
    assert session.scalar(select(func.count(Application.id))) == 1
    assert job.application.notes == "Call recruiter"
    owner_reply = session.scalar(
        select(BotMessage).where(BotMessage.dedup_key == "1:42:update:3:0")
    )
    member_reply = session.scalar(
        select(BotMessage).where(BotMessage.dedup_key == "1:43:update:2:0")
    )
    assert "Call recruiter" in owner_reply.payload["text"]
    assert owner_reply.payload["chat_id"] == 42
    assert member_reply.payload["chat_id"] == 43


def test_member_cannot_manage_access_and_revocation_cancels_delivery(session, service):
    member = join(session, service)
    assert service.handle_update(incoming(2, 43, "/invite"))
    session.expire_all()
    assert service.controller.state(session, "invite") is None
    assert service.handle_update(incoming(3, 42, "/revoke 43"))
    assert not service.handle_update(incoming(4, 43, "/saved"))
    session.expire_all()
    assert service.controller.allowed_user_ids(session) == [42]
    assert prepare_message(session, member) is None
    statuses = session.scalars(
        select(BotMessage.status).where(BotMessage.dedup_key.startswith("1:43:"))
    ).all()
    assert statuses and set(statuses) == {"cancelled"}


def test_member_replay_after_global_cursor_interruption_is_idempotent(session, service):
    member = join(session, service)
    assert service.handle_update(incoming(2, 43, "/saved"))
    session.expire_all()
    count = session.scalar(select(func.count(BotMessage.id)))
    service.controller.set_state(session, "offset", 2)
    session.commit()
    assert service.handle_update(incoming(2, 43, "/saved"))
    session.expire_all()
    assert session.scalar(select(func.count(BotMessage.id))) == count
    assert member.state(session, "offset") == 3
    assert service.controller.state(session, "offset") == 3


def test_members_receive_independent_notifications_and_pause(session, service):
    member = join(session, service)
    job = Job(
        title="Finance Intern",
        company="Example",
        source="Test",
        match_score=90,
        original_url="https://example.com/notify",
        canonical_url="https://example.com/notify",
        job_hash="notify",
    )
    session.add(job)
    session.commit()
    for account in service.controllers(session):
        queue_notifications(session, account, utcnow())
    messages = session.scalars(
        select(BotMessage).where(BotMessage.dedup_key.endswith(f":job:{job.id}"))
    ).all()
    assert {message.payload["chat_id"] for message in messages} == {42, 43}
    assert service.handle_update(incoming(2, 43, "/pause"))
    session.expire_all()
    assert member.state(session, "paused")
    assert not service.controller.state(session, "paused")


def test_member_search_uses_one_shared_worker_and_notifies_manual_requester(session, service):
    member = join(session, service)
    assert service.handle_update(incoming(2, 43, "/search"))
    assert service.tick()
    session.expire_all()
    assert service.controller.state(session, "search_running")
    assert service.controller.state(session, "search_requesters") == [43]
    assert not member.state(session, "search_requested")
    service.search_task = object()
    assert service.handle_update(incoming(3, 42, "/search"))
    assert not service.tick()
    session.expire_all()
    assert service.controller.state(session, "search_requesters") == [42, 43]
    member.set_state(session, "paused", True)
    service.controller.set_state(session, "paused", True)
    service.notify_search(session, "search:100", "Search complete")
    session.commit()
    messages = session.scalars(
        select(BotMessage).where(BotMessage.dedup_key.endswith(":search:100"))
    ).all()
    assert {message.payload["chat_id"] for message in messages} == {42, 43}
