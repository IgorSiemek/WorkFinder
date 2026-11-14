import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.automation import workday, worker
from app.models import ApplicationDraft, BotMessage, Job, utcnow
from app.services import applications
from app.services.bot_config import validate_config
from app.telegram.controller import BotController
from app.telegram.views import job_card


def test_browser_launch_enables_sandbox_and_excludes_application_secrets(tmp_path, monkeypatch):
    captured = {}

    class CapturedLaunch(Exception):
        pass

    class Chromium:
        async def launch(self, **options):
            captured.update(options)
            raise CapturedLaunch

    class Playwright:
        async def start(self):
            return SimpleNamespace(chromium=Chromium())

    monkeypatch.setattr(workday, "async_playwright", Playwright)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fixture-secret")
    monkeypatch.setenv("UNRELATED_PRIVATE_SETTING", "fixture-secret")
    job = SimpleNamespace(
        source="Motorola Solutions Careers",
        company="Motorola Solutions",
        original_url="https://motorolasolutions.wd5.myworkdayjobs.com/en-US/Careers/job/Krakow/Intern_R1",
    )
    with pytest.raises(CapturedLaunch):
        asyncio.run(workday.WorkdayForm().start(job, {}, {}, tmp_path))
    assert captured["chromium_sandbox"] is True
    assert "TELEGRAM_BOT_TOKEN" not in captured["env"]
    assert "UNRELATED_PRIVATE_SETTING" not in captured["env"]


@pytest.fixture
def application_service(session, tmp_path, monkeypatch):
    controller = BotController(42, validate_config({"search_times": []}))
    controller.set_state(
        session,
        "applicant",
        {
            "first_name": "Anna",
            "last_name": "Example",
            "email": "anna@example.com",
            "phone": "+48123456789",
            "country": "Poland",
        },
    )
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF-1.4\n%%EOF")
    controller.set_state(session, "application_cv", {"path": str(cv), "name": "CV.pdf"})
    job = Job(
        source="Motorola Solutions Careers",
        company="Motorola Solutions",
        title="Finance Intern",
        original_url="https://motorolasolutions.wd5.myworkdayjobs.com/en-US/Careers/job/Krakow/Finance-Intern_R1",
        canonical_url="https://example.com/job",
        job_hash="application-test",
        location="Krakow",
    )
    session.add(job)
    session.commit()
    sessions = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    service = worker.ApplicationWorker(None, controller, sessions)
    service.directory = tmp_path / "applications"
    return SimpleNamespace(controller=controller, job=job, worker=service)


def incoming(identifier, text):
    return {
        "update_id": identifier,
        "message": {
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "text": text,
        },
    }


def test_prepare_freezes_profile_and_cv_and_preserves_manual_apply(session, application_service):
    service = application_service
    card = job_card(service.job)
    callbacks = [
        button.get("callback_data")
        for row in card["reply_markup"]["inline_keyboard"]
        for button in row
    ]
    assert f"prepare:{service.job.id}" in callbacks
    assert f"apply:{service.job.id}" in callbacks
    draft = applications.prepare(service.controller, session, service.job.id)
    assert applications.prepare(service.controller, session, service.job.id) is draft
    assert service.job.application.status == "Preparing"
    applications.save_profile_value(service.controller, session, "first_name", "Changed")
    assert draft.profile["first_name"] == "Anna"
    assert draft.cv["name"] == "CV.pdf"


@pytest.mark.parametrize(
    "url",
    [
        "http://motorolasolutions.wd5.myworkdayjobs.com/job/1",
        "https://example.com/job/1",
        "https://motorolasolutions.wd5.myworkdayjobs.com.evil.example/job/1",
        "https://motorolasolutions.wd5.myworkdayjobs.com:444/job/1",
    ],
)
def test_only_registered_employer_https_urls_are_accepted(session, application_service, url):
    service = application_service
    service.job.original_url = url
    with pytest.raises(ValueError):
        applications.prepare(service.controller, session, service.job.id)
    assert service.job.application is None


def test_missing_profile_or_cv_is_reported_before_creating_draft(session, application_service):
    service = application_service
    service.controller.set_state(session, "applicant", {})
    with pytest.raises(ValueError, match="setup"):
        applications.prepare(service.controller, session, service.job.id)
    assert session.scalar(select(ApplicationDraft)) is None


def test_setup_and_shared_profile_persist_and_cancel(session, application_service):
    controller = application_service.controller
    controller.handle(session, incoming(1, "/setup"))
    controller.handle(session, incoming(2, "Alicja"))
    assert applications.shared_profile(controller.for_user(43), session)["first_name"] == "Alicja"
    assert controller.state(session, "setup_field") == "last_name"
    controller.handle(session, incoming(3, "/cancel"))
    assert controller.state(session, "setup_field") is None
    with pytest.raises(ValueError):
        applications.save_profile_value(controller, session, "email", "invalid")


def test_resume_notifications_and_cv_upload_have_separate_commands(session, application_service):
    controller = application_service.controller
    controller.handle(session, incoming(1, "/pause"))
    controller.handle(session, incoming(2, "/resume"))
    assert not controller.state(session, "paused")
    assert not controller.state(session, "awaiting_cv")
    controller.handle(session, incoming(3, "/uploadcv"))
    upload = incoming(4, "")
    upload["message"]["document"] = {
        "file_id": "example",
        "file_name": "CV.pdf",
        "file_size": 100,
    }
    controller.handle(session, upload)
    assert controller.state(session, "cv_upload")["file_id"] == "example"
    assert not controller.state(session, "awaiting_cv")


def test_review_messages_handle_long_answers_and_paginate_choices(session, application_service):
    service = application_service
    draft = applications.prepare(service.controller, session, service.job.id)
    draft.status = "review"
    draft.answers = {"Question": "<&>" * 3000}
    draft.snapshot = {
        "fields": [],
        "choosing": 1,
        "options": [{"label": str(index), "value": str(index)} for index in range(250)],
    }
    messages = applications.draft_messages(draft)
    assert all(len(message["text"]) <= 4096 for message in messages)
    keyboard = messages[-1]["reply_markup"]["inline_keyboard"]
    assert len(keyboard) < 30
    assert "choicepage:20" in str(keyboard)
    assert ":send" not in str(keyboard)


def test_remembered_answer_is_scoped_to_employer_and_frozen_per_draft(session, application_service):
    service = application_service
    draft = applications.prepare(service.controller, session, service.job.id)
    draft.status = "input"
    draft.snapshot = {"fields": [], "last_answer": {"label": "Work authorization*", "value": "yes"}}
    service.controller.callback(session, f"form:{draft.id}:{draft.revision}:remember")
    bank = service.controller.state(session, "applicant_answers")
    host = applications.workday_host(service.job)
    assert bank == {host: {"work authorization": "yes"}}
    assert draft.profile["answers"] == {}
    draft.status = "failed"
    applications.prepare(service.controller, session, service.job.id)
    assert draft.profile["answers"] == {"work authorization": "yes"}


def test_send_requires_current_review_and_is_idempotent(session, application_service):
    service = application_service
    draft = applications.prepare(service.controller, session, service.job.id)
    draft.status = "input"
    with pytest.raises(ValueError, match="podsumowanie"):
        applications.request_action(service.controller, session, draft.id, draft.revision, "send")
    draft.status = "review"
    with pytest.raises(ValueError, match="wygasł"):
        applications.request_action(
            service.controller, session, draft.id, draft.revision - 1, "send"
        )
    applications.request_action(service.controller, session, draft.id, draft.revision, "send")
    with pytest.raises(ValueError, match="Poczekaj"):
        applications.request_action(service.controller, session, draft.id, draft.revision, "send")
    with pytest.raises(ValueError):
        applications.request_action(
            service.controller.for_user(43), session, draft.id, draft.revision, "send"
        )
    assert service.job.application.status == "Preparing"


class Form:
    calls = []
    fail_submission = False

    async def start(self, job, profile, cv, directory):
        self.calls.append("start")
        return {"fields": [], "text": "Review", "submit": True, "cv_attached": True}

    async def validate_review(self, snapshot, profile, cv):
        self.calls.append("validate")

    async def submit(self):
        self.calls.append("submit")
        if self.fail_submission:
            raise TimeoutError("No confirmation")
        return "https://example.com/confirmation\nThank you for applying"

    async def close(self):
        pass


@pytest.mark.parametrize("confirmed", [True, False])
def test_worker_submits_only_after_review_and_never_retries_uncertain_send(
    session,
    application_service,
    monkeypatch,
    confirmed,
):
    service = application_service
    form = Form()
    form.calls = []
    form.fail_submission = not confirmed
    monkeypatch.setattr(worker, "WorkdayForm", lambda: form)
    draft = applications.prepare(service.controller, session, service.job.id)
    session.commit()
    asyncio.run(service.worker.step())
    session.expire_all()
    assert draft.status == "review"
    assert form.calls == ["start"]
    assert service.job.application.status == "Preparing"
    applications.request_action(service.controller, session, draft.id, draft.revision, "send")
    session.commit()
    asyncio.run(service.worker.step())
    session.expire_all()
    assert form.calls == ["start", "validate", "submit"]
    assert draft.status == ("submitted" if confirmed else "uncertain")
    assert service.job.application.status == ("Applied" if confirmed else "Preparing")
    asyncio.run(service.worker.step())
    assert form.calls.count("submit") == 1
    assert bool(draft.receipt) == confirmed


def test_restart_and_revocation_prevent_automatic_submission(session, application_service):
    service = application_service
    draft = applications.prepare(service.controller, session, service.job.id)
    draft.status = "submitting"
    session.commit()
    service.worker.recover()
    session.expire_all()
    assert draft.status == "uncertain"
    assert service.job.application.status == "Preparing"
    draft.status = "review"
    draft.user_id = 43
    draft.updated_at = utcnow() - timedelta(minutes=1)
    session.commit()
    asyncio.run(service.worker.step())
    session.expire_all()
    assert draft.status == "cancelled"


def test_cv_upload_checks_content_and_preserves_previous_cv(session, application_service):
    service = application_service

    class Client:
        data = b"%PDF-1.7\nExample CV\n%%EOF"

        async def download_file(self, *args):
            return self.data

    client = Client()
    service.worker.client = client
    service.controller.set_state(
        session,
        "cv_upload",
        {
            "user_id": 42,
            "file_id": "test",
            "name": "../CV.pdf",
            "update_id": 9,
        },
    )
    session.commit()
    asyncio.run(service.worker.upload_cv())
    session.expire_all()
    cv = service.controller.state(session, "application_cv")
    assert cv["path"].endswith(".pdf") and ".." not in cv["path"]
    assert service.controller.state(session, "cv_upload") is None
    client.data = b"not a PDF"
    service.controller.set_state(
        session,
        "cv_upload",
        {
            "user_id": 42,
            "file_id": "bad",
            "name": "CV.pdf",
            "update_id": 10,
        },
    )
    session.commit()
    asyncio.run(service.worker.upload_cv())
    session.expire_all()
    assert service.controller.state(session, "application_cv") == cv


def test_password_values_are_masked_in_review_messages(session, application_service):
    service = application_service
    draft = applications.prepare(service.controller, session, service.job.id)
    draft.status = "review"
    draft.snapshot = {
        "fields": [{"id": 1, "label": "Password", "value": "secret", "type": "password"}]
    }
    assert "secret" not in str(applications.draft_messages(draft))
    assert session.scalar(select(BotMessage)) is None
