import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import cli
from app.database import Base
from app.models import BotState, Job, utcnow
from app.services.bot_config import validate_config
from app.telegram import runtime
from app.telegram.controller import BotController


def test_runtime_handles_updates_and_delivery_with_persistent_database(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'bot.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(runtime, "SessionLocal", sessions)
    monkeypatch.setattr(runtime, "automatic_backup", lambda: None)
    controller = BotController(42, validate_config({"search_times": [], "minimum_score": 0}))
    updates = [
        {
            "update_id": 1,
            "message": {
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "text": "/start",
            },
        },
        {
            "update_id": 2,
            "message": {
                "from": {"id": 42},
                "chat": {"id": 42, "type": "private"},
                "text": "/add Data Intern | Example | https://example.com/runtime | Hybrid | Excel",
            },
        },
        {
            "update_id": 3,
            "callback_query": {
                "id": "c3",
                "from": {"id": 42},
                "data": "apply:1",
                "message": {"message_id": 500, "chat": {"id": 42, "type": "private"}},
            },
        },
    ]

    async def exercise():
        delivered = asyncio.Event()

        class Client:
            async def call(self, method, **payload):
                if method == "getUpdates":
                    if updates:
                        result = updates[:]
                        updates.clear()
                        return result
                    await asyncio.Event().wait()
                if "Status · Aplikacja wysłana" in payload.get("text", ""):
                    delivered.set()
                return {"message_id": 500}

        task = asyncio.create_task(runtime.BotRuntime(Client(), controller).run())
        try:
            await asyncio.wait_for(delivered.wait(), timeout=10)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    try:
        asyncio.run(exercise())
        with sessions() as session:
            job = session.scalar(select(Job))
            assert job.title == "Data Intern"
            assert job.application.status == "Applied"
            assert controller.state(session, "offset") == 4
            assert controller.state(session, "last_poll")
    finally:
        engine.dispose()


@pytest.mark.parametrize("fresh", [True, False])
def test_healthcheck_requires_recent_poll_from_current_build(session, monkeypatch, fresh):
    settings = SimpleNamespace(telegram_user_id="42", build_ref="release")
    monkeypatch.setattr(cli, "settings", settings)
    monkeypatch.setattr(cli, "SessionLocal", sessionmaker(bind=session.get_bind()))
    monkeypatch.setattr("sys.argv", ["workfinder", "healthcheck"])
    polled = utcnow() - timedelta(seconds=10 if fresh else 120)
    session.add_all(
        [
            BotState(key="1:42:last_poll", value=polled.isoformat()),
            BotState(key="1:42:build_ref", value="release"),
        ]
    )
    session.commit()
    if fresh:
        cli.main()
        settings.build_ref = "other-release"
    with pytest.raises(SystemExit) as failure:
        cli.main()
    assert failure.value.code == 1


def test_due_search_is_persistent_and_repeated_requests_coalesce(session, monkeypatch):
    sessions = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(runtime, "SessionLocal", sessions)
    controller = BotController(42, validate_config({"search_times": []}))
    controller.set_state(session, "started_at", utcnow().isoformat())
    controller.set_state(session, "next_search", (utcnow() - timedelta(minutes=1)).isoformat())
    session.commit()
    service = runtime.BotRuntime(None, controller)
    assert service.tick()
    session.expire_all()
    assert controller.state(session, "search_running")
    service.search_task = object()
    controller.set_state(session, "search_requested", True)
    session.commit()
    assert not service.tick()
    session.expire_all()
    assert not controller.state(session, "search_requested")


def test_backup_cli_works_when_json_is_invalid(session, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "SessionLocal", sessionmaker(bind=session.get_bind()))
    monkeypatch.setattr(cli, "automatic_backup", lambda: None)
    monkeypatch.setattr(cli, "init_database", lambda: None)
    monkeypatch.setattr(cli, "backup_database", lambda **kwargs: tmp_path / "backup.sqlite3")
    monkeypatch.setattr("sys.argv", ["workfinder", "backup"])

    def invalid_config():
        raise ValueError("invalid JSON")

    monkeypatch.setattr(runtime, "initialize", invalid_config)
    cli.main()
    assert "backup.sqlite3" in capsys.readouterr().out
