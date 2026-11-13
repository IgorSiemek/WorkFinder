import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app import database
from app.automation.worker import ApplicationWorker
from app.database import Base, SessionLocal
from app.models import BotMessage, CandidateProfile, CompanyPreference, Job, SavedSearch, utcnow
from app.profiles.context import activate_profile, current_profile
from app.profiles.controller import PersonalController
from app.profiles.router import ProfileRouter
from app.profiles.scope import job_filter, role_matches, validate_scope
from app.profiles.sources import PersonalWorkdaySource
from app.profiles.store import ProfileStore, validate_personal_config
from app.scrapers.base import JobPosting, SearchCriteria
from app.scrapers.registry import enabled_sources
from app.scrapers.workday import WorkdayConfig, WorkdaySource
from app.services import bot_config, search
from app.services.backups import backup_database, backup_directory
from app.services.jobs import get_job, list_jobs
from app.telegram import runtime
from app.telegram.controller import BotController
from app.telegram.delivery import deliver_one, queue_notifications


def make_job(**values):
    return Job(job_hash="fixture", **values)


def personal_config(**scope):
    return {
        "profile": {"name": "Friend", "skills": ["Python"], "preferred_jobs": ["Software"]},
        "job_scope": {
            "role_type": "regular",
            "locations": ["Kraków"],
            "titles": ["Software"],
            **scope,
        },
        "sources": ["Motorola Solutions Careers"],
        "minimum_score": 0,
        "search_times": [],
        "searches": [],
        "companies": [],
    }


def update(identifier, user, text=None, callback=None, chat_type="private"):
    message = {"from": {"id": user}, "chat": {"id": user, "type": chat_type}, "text": text or ""}
    if callback:
        return {
            "update_id": identifier,
            "callback_query": {
                "id": f"c{identifier}",
                "from": {"id": user},
                "data": callback,
                "message": {"message_id": identifier, "chat": message["chat"]},
            },
        }
    return {"update_id": identifier, "message": message}


@pytest.fixture
def original(tmp_path, monkeypatch):
    path = tmp_path / "original.db"
    bind = create_engine(f"sqlite:///{path.as_posix()}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind)
    sessions = sessionmaker(bind=bind, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(database, "_default_sessions", sessions)
    monkeypatch.setattr(
        bot_config, "settings", SimpleNamespace(config_path=str(tmp_path / "config.json"))
    )
    config = bot_config.validate_config({"minimum_score": 0, "search_times": []})
    bot_config.config_path().write_text(json.dumps(config), encoding="utf8")
    controller = BotController(42, config, bot_username="ExampleBot")
    with sessions() as session:
        session.add(CandidateProfile(name="Original student", skills=["Excel"]))
        session.add_all(
            [
                make_job(
                    source="Manual",
                    title="Finance Intern",
                    company="OriginalCo",
                    location="Kraków",
                    canonical_url="https://example.com/shared",
                    original_url="https://example.com/shared",
                    description="Original private note",
                    match_score=95,
                    notes="private-original",
                ),
                make_job(
                    source="Manual",
                    title="Data Intern",
                    company="OriginalCo",
                    location="Kraków",
                    canonical_url="https://example.com/only-original",
                    original_url="https://example.com/only-original",
                    match_score=85,
                ),
            ]
        )
        controller.set_state(session, "members", [43])
        controller.set_state(session, "started_at", utcnow().isoformat())
        session.commit()
    yield controller, sessions
    bind.dispose()


def make_profile(tmp_path, key="friend", **scope):
    store = ProfileStore(tmp_path / "profiles")
    token = store.create(key, personal_config(**scope))
    context, config = store.open(key)
    return store, token, context, config


def test_provisioning_does_not_change_original_database_or_json(original, tmp_path):
    controller, sessions = original
    before = bot_config.config_path().read_bytes()
    store, token, context, _ = make_profile(tmp_path)
    try:
        assert token.encode() not in store.path.read_bytes()
        assert bot_config.config_path().read_bytes() == before
        with sessions() as session:
            assert session.scalar(select(func.count(Job.id))) == 2
            assert session.scalar(select(CandidateProfile)).name == "Original student"
            assert controller.allowed_user_ids(session) == [42, 43]
        with activate_profile(context), SessionLocal() as session:
            assert session.scalar(select(func.count(Job.id))) == 0
            assert session.scalar(select(func.count(SavedSearch.id))) == 0
            assert session.scalar(select(func.count(CompanyPreference.id))) == 0
            profile = session.scalar(select(CandidateProfile))
            assert profile.name == "Friend" and profile.skills == ["Python"]
            assert profile.education == "" and profile.technologies == []
            assert bot_config.config_path() == context.directory / "config.json"
        assert current_profile() is None
    finally:
        context.engine.dispose()


def test_invite_is_single_use_atomic_and_excludes_existing_users(tmp_path):
    store, token, context, _ = make_profile(tmp_path)
    context.engine.dispose()
    assert store.claim(token, 42, reserved={42, 43}) is None
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda user: store.claim(token, user, reserved={42, 43}), [100, 101])
        )
    assert sum(result is not None for result in results) == 1
    winner = next(result["user_id"] for result in results if result)
    assert store.claim(token, 102, reserved=set()) is None
    other = store.create("other", personal_config())
    assert store.claim(other, winner, reserved=set()) is None
    assert store.claim(other, 200, reserved=set())["profile_key"] == "other"


def test_expired_or_rotated_code_cannot_be_used(tmp_path, monkeypatch):
    store, token, context, _ = make_profile(tmp_path)
    context.engine.dispose()
    replacement = store.invite("friend")
    assert store.claim(token, 100, reserved=set()) is None
    monkeypatch.setattr("app.profiles.store.time.time", lambda: 10**12)
    assert store.claim(replacement, 100, reserved=set()) is None


@pytest.mark.parametrize("key", ["../default", "default", "legacy", "x/y", "C:\\test", ".", "A"])
def test_profile_key_cannot_address_original_files(tmp_path, key):
    store = ProfileStore(tmp_path / "profiles")
    with pytest.raises(ValueError):
        store.create(key, personal_config())
    assert not store.exists()


def test_existing_profile_is_never_overwritten(tmp_path):
    store, _, context, _ = make_profile(tmp_path)
    context.engine.dispose()
    path = store.directory("friend") / "config.json"
    before = path.read_bytes()
    with pytest.raises(ValueError, match="już istnieje"):
        store.create("friend", personal_config(titles=["Different"]))
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "scope",
    [
        {"role_type": []},
        {"role_type": "admin"},
        {"remote_allowed": "yes"},
        {"locations": [], "remote_allowed": False},
        {"titles": [3]},
        {"path": "../config.json"},
    ],
)
def test_scope_validation(scope):
    with pytest.raises(ValueError):
        validate_scope(scope)


def test_personal_profile_requires_explicit_sources_and_scope():
    for config in [
        {"profile": {"name": "Friend"}},
        personal_config() | {"sources": []},
        personal_config() | {"sources": ["HAVI Careers"]},
    ]:
        with pytest.raises(ValueError):
            validate_personal_config(config)


def test_scope_and_search_threads_are_isolated_from_original(original, tmp_path, monkeypatch):
    _, sessions = original
    _, _, context, _ = make_profile(tmp_path)
    captured = []

    class Source:
        name = "Fixture"
        can_confirm_absence = True
        scan_complete = True

        def search_jobs(self, criteria):
            captured.append((current_profile().key, criteria.keywords, criteria.locations))
            return [
                JobPosting(
                    source=self.name,
                    source_job_id=str(i),
                    url=f"https://example.com/{i}",
                    title=title,
                    company="PrivateCo",
                    location=city,
                    date_posted=date.today(),
                    description="Python software engineering",
                )
                for i, title, city in [
                    (1, "Software Engineer", "Krakow"),
                    (2, "Software Intern", "Krakow"),
                    (3, "Software Engineer", "Warsaw"),
                ]
            ]

    monkeypatch.setattr(search, "enabled_sources", lambda: [Source()])
    monkeypatch.setattr(search, "refresh_posting_dates", lambda session: None)
    try:
        with activate_profile(context), SessionLocal() as session:
            result = search.run_search(session, source_names=["Fixture"])
            assert result.errors == 0 and result.jobs_found == 1
            jobs, total = list_jobs(session, "all")
            assert total == 1 and jobs[0].title == "Software Engineer"
            assert jobs[0].match_score >= 75
            assert captured == [("friend", ["Software"], ["Kraków"])]
            assert not role_matches("Software Intern")
            assert not role_matches("Senior Software Intern")
            assert role_matches("Software Engineer")
        with sessions() as session:
            assert {job.title for job in list_jobs(session, "all")[0]} == {
                "Finance Intern",
                "Data Intern",
            }
        assert role_matches("Finance Intern") and not role_matches("Software Engineer")
    finally:
        context.engine.dispose()


def test_workday_regular_roles_remote_and_multiple_locations_are_configurable(
    tmp_path, monkeypatch
):
    _, _, context, _ = make_profile(tmp_path, locations=["Warszawa"], remote_allowed=True)
    queries = []

    def post(url, **kwargs):
        queries.append(kwargs["json"]["searchText"])
        records = [
            {"title": title, "locationsText": city, "externalPath": f"/job/location/role_{i}"}
            for i, title, city in [
                (1, "Software Engineer", "Warsaw"),
                (2, "Software Intern", "Warsaw"),
                (3, "Software Engineer", "Berlin"),
            ]
        ]
        return httpx.Response(
            200, request=httpx.Request("POST", url), json={"total": 3, "jobPostings": records}
        )

    monkeypatch.setattr("app.profiles.sources.httpx.post", post)
    monkeypatch.setattr(
        PersonalWorkdaySource,
        "posting_details",
        lambda self, path: {
            "jobDescription": "Python software",
            "location": "Warsaw" if path.endswith("1") else "Berlin",
            "remoteType": "Remote" if path.endswith("3") else "Hybrid",
            "startDate": "2026-01-01",
        },
    )
    try:
        with activate_profile(context):
            source = PersonalWorkdaySource(
                WorkdayConfig("Example", "Example", "example.com", "t", "s")
            )
            jobs = source.search_jobs(SearchCriteria())
            assert [job.source_job_id for job in jobs] == ["1", "3"]
            assert queries == ["warsaw", "Software"]
            assert jobs[1].remote_type == "Remote" and source.scan_complete
            assert all(
                isinstance(s, (PersonalWorkdaySource,)) or s.name == "Arbeitnow"
                for s in enabled_sources()
            )
        assert any(type(source) is WorkdaySource for source in enabled_sources())
    finally:
        context.engine.dispose()


def test_cv_backups_notifications_and_scheduler_use_private_database(original, tmp_path):
    legacy, sessions = original
    _, _, context, config = make_profile(tmp_path)
    private = PersonalController(100, config)

    class Client:
        async def download_file(self, file_id, limit):
            return b"%PDF-1.4 private candidate CV"

    try:
        with sessions() as session:
            legacy.set_state(session, "application_cv", {"path": "original-cv.pdf"})
            session.commit()
        with activate_profile(context):
            worker = ApplicationWorker(Client(), private, SessionLocal)
            assert worker.directory == context.directory / "applications"
            with SessionLocal() as session:
                private.set_state(
                    session,
                    "cv_upload",
                    {"user_id": 100, "file_id": "f", "name": "cv.pdf", "update_id": 5},
                )
                private.set_state(session, "started_at", "2026-01-01T00:00:00")
                private.set_state(session, "search_requested", True)
                session.add(
                    make_job(
                        source="Manual",
                        title="Software Engineer",
                        company="PrivateCo",
                        canonical_url="https://example.com/shared",
                        original_url="https://example.com/shared",
                        location="Krakow",
                        match_score=90,
                    )
                )
                session.commit()
            asyncio.run(worker.upload_cv())
            assert runtime.BotRuntime(None, private).tick()
            with SessionLocal() as session:
                cv = private.state(session, "application_cv")
                assert hashlib.sha256(Path(cv["path"]).read_bytes()).hexdigest() == cv["sha256"]
                queue_notifications(session, private, utcnow())
                messages = session.scalars(select(BotMessage)).all()
                assert messages and all(m.payload["chat_id"] == 100 for m in messages)
                assert all("OriginalCo" not in json.dumps(m.payload) for m in messages)
            backup = backup_database(force=True)
            assert backup.parent == backup_directory() == context.directory / "backups"
        with sessions() as session:
            assert legacy.state(session, "application_cv") == {"path": "original-cv.pdf"}
            assert legacy.state(session, "search_requested") is None
            assert session.scalar(select(func.count(BotMessage.id))) == 0
    finally:
        context.engine.dispose()


def test_router_keeps_legacy_shared_access_and_private_callbacks_separate(
    original, tmp_path, monkeypatch
):
    legacy, sessions = original
    store, token, initial, _ = make_profile(tmp_path)
    initial.engine.dispose()
    sent = []

    class Client:
        async def call(self, method, **payload):
            sent.append((method, payload))
            return {"message_id": 1}

    async def idle(self, runtime):
        await asyncio.Event().wait()

    monkeypatch.setattr(ProfileRouter, "serve", idle)
    service = runtime.BotRuntime(Client(), legacy)
    router = ProfileRouter(service, store)

    async def exercise():
        try:
            assert await router.dispatch(update(1, 43, "/start"))
            assert await router.dispatch(update(2, 100, f"/start profile_{token}"))
            context, child, _ = router.children["friend"]
            with activate_profile(context), SessionLocal() as session:
                session.add(
                    make_job(
                        source="Manual",
                        title="Software Engineer",
                        company="PrivateCo",
                        canonical_url="https://example.com/shared",
                        original_url="https://example.com/shared",
                        location="Krakow",
                        match_score=90,
                    )
                )
                session.commit()
            assert await router.dispatch(update(3, 100, callback="apply:1"))
            assert await router.dispatch(update(4, 100, "/job 2"))
            assert await router.dispatch(update(5, 43, callback="save:1"))
            assert not await router.dispatch(update(6, 101, f"/start profile_{token}"))
            assert not await router.dispatch(update(7, 100, "/all", chat_type="group"))
            with activate_profile(context), SessionLocal() as session:
                assert get_job(session, 1).application.status == "Applied"
                assert any(
                    "Nie znaleziono oferty" in m.payload.get("text", "")
                    for m in session.scalars(select(BotMessage))
                )
            with sessions() as session:
                assert get_job(session, 1).application.status == "Saved"
                assert get_job(session, 1).notes == "private-original"
                assert legacy.allowed_user_ids(session) == [42, 43]
                assert legacy.state(session, "offset") == 8
                assert all(
                    m.payload["chat_id"] in {42, 43} for m in session.scalars(select(BotMessage))
                )
            assert sent and sent[-1][1]["chat_id"] == 101
            with activate_profile(context):
                await deliver_one(service.client, SessionLocal, child.controller)
            assert sent[-1][1]["chat_id"] == 100
        finally:
            for context, _, task in router.children.values():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                context.engine.dispose()

    asyncio.run(exercise())


def test_queued_private_jobs_are_rechecked_after_requirements_change(tmp_path):
    _, _, context, config = make_profile(tmp_path)
    controller = PersonalController(100, config)
    try:
        with activate_profile(context), SessionLocal() as session:
            session.add(
                make_job(
                    source="Manual",
                    title="Software Engineer",
                    company="PrivateCo",
                    canonical_url="https://example.com/x",
                    original_url="https://example.com/x",
                    location="Krakow",
                    match_score=90,
                )
            )
            controller.set_state(session, "started_at", "2026-01-01T00:00:00")
            session.commit()
            queue_notifications(session, controller, utcnow())
            context.scope = validate_scope({"role_type": "regular", "titles": ["Accountant"]})
            assert session.scalar(select(Job.id).where(job_filter())) is None
            from app.telegram.delivery import prepare_message

            assert prepare_message(session, controller) is None
            assert session.scalar(select(BotMessage)).status == "cancelled"
    finally:
        context.engine.dispose()


def test_friend_json_cannot_be_applied_to_original_profile(original):
    _, sessions = original
    with sessions() as session:
        with pytest.raises(ValueError, match="profile-create"):
            bot_config.apply_config(session, personal_config())
        assert session.scalar(select(CandidateProfile)).name == "Original student"


def test_broken_registry_does_not_block_existing_accounts(original, tmp_path, monkeypatch):
    legacy, sessions = original
    store = ProfileStore(tmp_path / "profiles")
    router = ProfileRouter(runtime.BotRuntime(None, legacy), store)

    def broken(*args):
        raise OSError("damaged private registry")

    monkeypatch.setattr(store, "for_user", broken)

    async def exercise():
        assert await router.dispatch(update(1, 43, "/start"))
        assert not await router.dispatch(update(2, 100, "/all"))
        assert await router.dispatch(update(3, 42, "/job 1"))

    asyncio.run(exercise())
    with sessions() as session:
        assert legacy.state(session, "offset") == 4
        assert any(
            "OriginalCo" in m.payload.get("text", "") for m in session.scalars(select(BotMessage))
        )


def test_two_private_async_tasks_do_not_share_context_or_rows(tmp_path):
    store, _, context1, _ = make_profile(tmp_path, key="one")
    store.create("two", personal_config())
    context2, _ = store.open("two")

    async def exercise(context, title):
        with activate_profile(context):
            await asyncio.sleep(0)

            def write_and_read():
                assert current_profile().key == context.key
                with SessionLocal() as session:
                    session.add(
                        make_job(
                            source="Manual",
                            title=title,
                            company=context.key,
                            canonical_url="https://example.com/same",
                            original_url="https://example.com/same",
                        )
                    )
                    session.commit()
                    return session.scalar(select(Job)).title

            return await asyncio.to_thread(write_and_read)

    async def together():
        return await asyncio.gather(exercise(context1, "First"), exercise(context2, "Second"))

    try:
        assert asyncio.run(together()) == ["First", "Second"]
        assert current_profile() is None
    finally:
        context1.engine.dispose()
        context2.engine.dispose()


def test_full_runtime_has_one_poller_and_separate_delivery(original, tmp_path, monkeypatch):
    legacy, sessions = original
    store, token, initial, _ = make_profile(tmp_path)
    initial.engine.dispose()
    updates = [
        update(1, 43, "/start"),
        update(2, 100, f"/start profile_{token}"),
        update(
            3,
            100,
            "/add Software Engineer | PrivateCo | https://example.com/private | Hybrid | Python",
        ),
        update(4, 100, callback="apply:1"),
        update(5, 42, "/job 1"),
    ]
    sent, pollers = [], set()
    monkeypatch.setattr(runtime, "automatic_backup", lambda: None)

    async def exercise():
        done = asyncio.Event()

        class Client:
            async def call(self, method, **payload):
                if method == "getUpdates":
                    pollers.add(asyncio.current_task())
                    if updates:
                        result = updates[:]
                        updates.clear()
                        return result
                    await asyncio.Event().wait()
                if method in {"sendMessage", "editMessageText"}:
                    sent.append(payload)
                    applied = any(
                        p.get("chat_id") == 100
                        and "Status · Aplikacja wysłana" in p.get("text", "")
                        for p in sent
                    )
                    original = any(
                        p.get("chat_id") == 42 and "OriginalCo" in p.get("text", "") for p in sent
                    )
                    if applied and original:
                        done.set()
                return {"message_id": 1}

        bot = runtime.BotRuntime(Client(), legacy)
        bot.profile_router = ProfileRouter(bot, store)
        task = asyncio.create_task(bot.run())
        try:
            await asyncio.wait_for(done.wait(), timeout=15)
            assert not task.done()
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    asyncio.run(exercise())
    assert len(pollers) == 1
    assert all(p["chat_id"] in {42, 43} for p in sent if "OriginalCo" in p.get("text", ""))
    assert all(p["chat_id"] == 100 for p in sent if "PrivateCo" in p.get("text", ""))
    with sessions() as session:
        assert get_job(session, 1).application is None
        assert legacy.allowed_user_ids(session) == [42, 43]


@pytest.mark.parametrize("registry_exists", [False, True])
def test_profile_routing_is_opt_in_at_startup(tmp_path, monkeypatch, registry_exists):
    monkeypatch.setattr(
        bot_config, "settings", SimpleNamespace(config_path=str(tmp_path / "config.json"))
    )
    monkeypatch.setattr(
        runtime, "settings", SimpleNamespace(telegram_token="dummy", telegram_user_id="42")
    )
    if registry_exists:
        ProfileStore(tmp_path / "profiles").initialize()
    captured = []

    class Client:
        async def call(self, method, **payload):
            return {"id": 1, "username": "ExampleBot"} if method == "getMe" else {}

        async def close(self):
            pass

    async def run(self):
        captured.append(self.profile_router)

    monkeypatch.setattr(runtime, "TelegramClient", lambda token: Client())
    monkeypatch.setattr(runtime.BotRuntime, "run", run)
    asyncio.run(runtime.run_bot(bot_config.validate_config({})))
    assert (captured[0] is not None) is registry_exists
    assert ProfileStore(tmp_path / "profiles").exists() is registry_exists


def test_private_reload_validates_before_mutation_and_preserves_original(original, tmp_path):
    _, sessions = original
    _, _, context, config = make_profile(tmp_path)
    controller = PersonalController(100, config)
    try:
        with activate_profile(context), SessionLocal() as session:
            new = personal_config(titles=["Accountant"])
            (context.directory / "config.json").write_text(json.dumps(new), encoding="utf8")
            controller.reload(session)
            assert context.scope["titles"] == ["Accountant"]
            assert controller.config["job_scope"]["titles"] == ["Accountant"]
            (context.directory / "config.json").write_text(
                json.dumps(new | {"sources": ["HAVI Careers"]}), encoding="utf8"
            )
            with pytest.raises(ValueError):
                controller.reload(session)
            assert context.scope["titles"] == ["Accountant"]
        with sessions() as session:
            assert session.scalar(select(CandidateProfile)).name == "Original student"
            assert session.scalar(select(func.count(Job.id))) == 2
    finally:
        context.engine.dispose()


def test_bad_private_config_cannot_stop_original_profile(original, tmp_path):
    legacy, sessions = original
    store, token, context, _ = make_profile(tmp_path)
    context.engine.dispose()
    assert store.claim(token, 100, reserved={42, 43})
    (store.directory("friend") / "config.json").write_text("invalid-json", encoding="utf8")
    sent = []

    class Client:
        async def call(self, method, **payload):
            sent.append(payload)

    router = ProfileRouter(runtime.BotRuntime(Client(), legacy), store)

    async def exercise():
        assert not await router.dispatch(update(1, 100, "/all"))
        assert await router.dispatch(update(2, 43, "/job 1"))

    asyncio.run(exercise())
    assert sent[0]["chat_id"] == 100 and "niedostępny" in sent[0]["text"]
    with sessions() as session:
        assert get_job(session, 1).notes == "private-original"
        assert any(
            "OriginalCo" in m.payload.get("text", "") for m in session.scalars(select(BotMessage))
        )
