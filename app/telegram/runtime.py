from __future__ import annotations

import asyncio
import logging
import os
import signal
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

import httpx
from sqlalchemy import select

from app.automation.worker import ApplicationWorker
from app.config import settings
from app.database import SessionLocal, init_database
from app.models import SearchRun, utcnow
from app.services.backups import automatic_backup, backup_database
from app.services.bot_config import (
    apply_config,
    config_path,
    export_config,
    load_config,
    next_search,
)
from app.services.progress import retry_excel_sync
from app.services.publication_dates import refresh_posting_dates
from app.services.search import run_search
from app.services.seed import seed_defaults
from app.telegram.client import TelegramClient, TelegramError
from app.telegram.controller import BotController
from app.telegram.delivery import deliver_one, queue_notifications, recover_delivery

logger = logging.getLogger(__name__)


@contextmanager
def process_lock(path: Path):  # noqa: ANN201
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0)
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("WorkFinder jest już uruchomiony dla tego katalogu danych") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def initialize() -> dict:
    automatic_backup()
    init_database()
    with SessionLocal() as session:
        # Seed defaults once. Explicit empty JSON sections must survive later restarts.
        from app.models import CandidateProfile

        if session.scalar(select(CandidateProfile.id).limit(1)) is None:
            seed_defaults(session)
        if not config_path().exists():
            export_config(session)
        config = load_config()
        apply_config(session, config)
        session.commit()
    return config


class BotRuntime:
    def __init__(self, client: TelegramClient, controller: BotController):
        self.client = client
        self.controller = controller
        self.search_task: asyncio.Task | None = None
        self.profile_router = None

    def controllers(self, session) -> list[BotController]:  # noqa: ANN001
        return [
            self.controller.for_user(user_id)
            for user_id in self.controller.allowed_user_ids(session)
        ]

    def notify_search(self, session, key: str, text: str) -> None:  # noqa: ANN001
        requesters = self.controller.state(session, "search_requesters", [])
        for recipient in self.controllers(session):
            if recipient.state(session, "started_at") and (
                recipient.owner_id in requesters or not recipient.state(session, "paused")
            ):
                recipient.queue(session, key, {"text": text})

    def search(self) -> None:
        controller = self.controller
        with SessionLocal() as session:
            try:
                result = run_search(session, source_names=controller.config["sources"])
                self.notify_search(
                    session,
                    f"search:{result.id}",
                    (
                        "<b>Wyszukiwanie zakończone</b>\n"
                        f"Nowych ofert · {result.new_jobs}\nZnalezionych · {result.jobs_found}\n"
                        f"Błędów źródeł · {result.errors}\n"
                        "Przeglądaj pod /new. Stan źródeł pod /health."
                    ),
                )
            except Exception:
                session.rollback()
                logger.exception("Search failed")
                self.notify_search(
                    session,
                    f"search-error:{utcnow().isoformat()}",
                    "Wyszukiwanie przerwane. Sprawdź /health i logi procesu.",
                )
            finally:
                controller.set_state(session, "search_running", False)
                due = next_search(controller.config, utcnow())
                controller.set_state(session, "next_search", due.isoformat() if due else None)
                session.commit()

    def tick(self) -> bool:
        controller = self.controller
        with SessionLocal() as session:
            now = utcnow()
            recipients = self.controllers(session)
            reloads = [member for member in recipients if member.state(session, "reload_requested")]
            if reloads and not self.search_task:
                try:
                    controller.reload(session)
                    for member in reloads:
                        member.set_state(session, "reload_requested", False)
                        if member.owner_id != controller.owner_id:
                            member.queue(
                                session,
                                f"reload:{now.isoformat()}",
                                {
                                    "text": "Konfiguracja wczytana.",
                                },
                            )
                    session.commit()
                except (ValueError, OSError) as exc:
                    session.rollback()
                    for member in reloads:
                        member.set_state(session, "reload_requested", False)
                        member.queue(
                            session,
                            f"reload-error:{now.isoformat()}",
                            {
                                "text": "Błąd konfiguracji · " + escape(str(exc)[:1000]),
                            },
                        )
                    session.commit()
            recipients = self.controllers(session)
            for member in recipients:
                queue_notifications(session, member, now)
            if not any(member.state(session, "started_at") for member in recipients):
                return False
            due = controller.state(session, "next_search")
            if due is None:
                next_due = next_search(controller.config, now)
                controller.set_state(
                    session, "next_search", next_due.isoformat() if next_due else None
                )
            scheduled = due is not None and datetime.fromisoformat(due) <= now
            requesters = [
                member.owner_id
                for member in recipients
                if member.state(session, "search_requested", False)
            ]
            requested = bool(requesters)
            start = (scheduled or requested) and self.search_task is None
            if start:
                controller.set_state(session, "search_requested", False)
                controller.set_state(session, "search_running", True)
                controller.set_state(session, "search_started", now.isoformat())
                controller.set_state(session, "search_manual", bool(requested))
                controller.set_state(session, "search_requesters", requesters)
            elif requested and self.search_task:
                # Repeated taps during an active search coalesce into that search.
                controller.set_state(session, "search_requested", False)
                previous = controller.state(session, "search_requesters", [])
                controller.set_state(
                    session, "search_requesters", sorted(set(previous + requesters))
                )
            for member in recipients:
                if member.owner_id in requesters:
                    member.set_state(session, "search_requested", False)
            session.commit()
            return start

    def maintenance(self) -> None:
        with SessionLocal() as session:
            retry_excel_sync(session)
            for member in self.controllers(session):
                self.maintain_member(session, member)
            automatic_backup()

    def maintain_member(self, session, controller: BotController) -> None:  # noqa: ANN001
        for command in ("backup", "excel", "dates"):
            value = controller.state(session, f"{command}_requested")
            if not value:
                continue
            try:
                if command == "backup":
                    path = backup_database(force=True)
                    text = f"Backup zapisany · {path.name if path else 'brak pliku bazy'}"
                elif command == "excel":
                    done, pending = retry_excel_sync(session)
                    text = f"Excel · zapisano {done}, oczekuje {pending}"
                else:
                    result = refresh_posting_dates(session, job_id=value, force=True)
                    text = (
                        f"Data publikacji · potwierdzone {result['verified']}, "
                        f"szacowane {result['relative']}, niedostępne {result['unknown']}"
                    )
            except Exception:
                session.rollback()
                logger.exception("Maintenance failed: %s", command)
                text = f"Zadanie {command} przerwane. Szczegóły w logach."
            controller.set_state(session, f"{command}_requested", False)
            controller.queue(
                session,
                f"task:{command}:{utcnow().isoformat()}",
                {
                    "text": escape(text),
                },
            )
            session.commit()

    async def poll(self) -> None:
        while True:
            with SessionLocal() as session:
                offset = self.controller.state(session, "offset", 0)
            updates = await self.client.call(
                "getUpdates",
                offset=offset,
                timeout=25,
                allowed_updates=["message", "callback_query"],
            )
            with SessionLocal() as session:
                self.controller.set_state(session, "last_poll", utcnow().isoformat())
                session.commit()
            for update in updates:
                authorized = (
                    await self.profile_router.dispatch(update)
                    if self.profile_router is not None
                    else await asyncio.to_thread(self.handle_update, update)
                )
                callback = update.get("callback_query")
                if callback and authorized:
                    with suppress(TelegramError, httpx.HTTPError):
                        await self.client.call(
                            "answerCallbackQuery", callback_query_id=callback["id"]
                        )

    def handle_update(self, update: dict) -> bool:
        with SessionLocal() as session:
            controller = self.controller
            if update["update_id"] < controller.state(session, "offset", 0):
                return False
            callback = update.get("callback_query")
            message = callback if callback else update.get("message", {})
            user_id = message.get("from", {}).get("id")
            if user_id not in controller.allowed_user_ids(session):
                if not controller.claim_invite(session, update):
                    controller.set_state(session, "offset", update["update_id"] + 1)
                    session.commit()
                    return False
                recover_delivery(session, controller.for_user(user_id))
            member = controller.for_user(user_id)
            if not member.authorized(update):
                controller.set_state(session, "offset", update["update_id"] + 1)
                session.commit()
                return False
            member.handle(session, update)
            if user_id != controller.owner_id:
                controller.set_state(session, "offset", update["update_id"] + 1)
                session.commit()
            return True

    async def deliver(self) -> None:
        while True:
            with SessionLocal() as session:
                recipients = self.controllers(session)
            for member in recipients:
                await deliver_one(self.client, SessionLocal, member)
            await asyncio.sleep(1.1)

    async def schedule(self) -> None:
        maintenance_at = utcnow()
        while True:
            if self.search_task and self.search_task.done():
                self.search_task.result()
                self.search_task = None
            if await asyncio.to_thread(self.tick):
                self.search_task = asyncio.create_task(asyncio.to_thread(self.search))
            if utcnow() >= maintenance_at:
                await asyncio.to_thread(self.maintenance)
                maintenance_at = utcnow() + timedelta(seconds=30)
            await asyncio.sleep(2)

    async def run(self, *, poll: bool = True) -> None:
        with SessionLocal() as session:
            for member in self.controllers(session):
                recover_delivery(session, member)
            self.controller.set_state(session, "last_poll", None)
            self.controller.set_state(session, "build_ref", settings.build_ref)
            if self.controller.state(session, "search_running"):
                started = self.controller.state(session, "search_started")
                if started:
                    for run in session.scalars(
                        select(SearchRun).where(
                            SearchRun.status == "running",
                            SearchRun.started_at >= datetime.fromisoformat(started),
                        )
                    ):
                        run.status = "interrupted"
                        run.completed_at = utcnow()
                self.controller.set_state(session, "search_running", False)
            session.commit()

        # Expected network failures restart their loop. Programming/database errors stop the
        # process so Docker's restart policy can recover without silently losing a worker.
        async def resilient(operation):  # noqa: ANN001, ANN202
            delay = 2
            while True:
                try:
                    await operation()
                except TelegramError as exc:
                    if exc.code in {401, 409}:
                        raise ValueError(
                            "Sprawdź token, webhook i inne uruchomione kopie bota"
                        ) from None
                    logger.warning("Telegram API error %s", exc.code)
                    await asyncio.sleep(min(60, exc.retry_after or delay))
                except httpx.HTTPError:
                    logger.warning("Telegram connection unavailable")
                    await asyncio.sleep(delay)
                delay = min(60, delay * 2)

        async with asyncio.TaskGroup() as group:
            if poll:
                group.create_task(resilient(self.poll))
            if self.profile_router is not None:
                group.create_task(self.profile_router.run())
            group.create_task(resilient(self.deliver))
            group.create_task(self.schedule())
            group.create_task(ApplicationWorker(self.client, self.controller, SessionLocal).run())


async def run_bot(config: dict) -> None:
    if not settings.telegram_token:
        raise ValueError("Ustaw TELEGRAM_BOT_TOKEN w .env")
    try:
        owner = int(settings.telegram_user_id)
    except ValueError as exc:
        raise ValueError("Ustaw TELEGRAM_USER_ID jako dodatni identyfikator konta") from exc
    if owner <= 0:
        raise ValueError("TELEGRAM_USER_ID musi być dodatni")
    client = TelegramClient(settings.telegram_token)
    try:
        identity = await client.call("getMe")
        webhook = await client.call("getWebhookInfo")
        if webhook.get("url"):
            raise ValueError("Bot ma aktywny webhook. Usuń go przed uruchomieniem long pollingu.")
        await client.call(
            "setMyCommands",
            commands=[
                {"command": command, "description": description}
                for command, description in (
                    ("start", "Menu WorkFinder"),
                    ("new", "Nowe oferty"),
                    ("saved", "Zapisane oferty"),
                    ("applications", "Moje aplikacje"),
                    ("search", "Szukaj teraz"),
                    ("followups", "Przypomnienia"),
                    ("setup", "Dane do aplikowania"),
                    ("uploadcv", "Prześlij CV"),
                    ("form", "Otwarty formularz"),
                    ("health", "Stan wyszukiwania"),
                    ("help", "Wszystkie polecenia"),
                    ("config", "Konfiguracja"),
                    ("reload", "Wczytaj plik JSON"),
                )
            ],
            scope={"type": "all_private_chats"},
        )
        logger.info("WorkFinder bot started")
        if os.name != "nt":
            task = asyncio.current_task()
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
        runtime = BotRuntime(
            client,
            BotController(
                owner,
                config,
                bot_id=identity["id"],
                bot_username=identity["username"],
            ),
        )
        from app.profiles.router import ProfileRouter
        from app.profiles.store import ProfileStore

        store = ProfileStore(config_path().parent / "profiles")
        if store.exists():
            runtime.profile_router = ProfileRouter(runtime, store)
        await runtime.run()
    finally:
        await client.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # Request URLs contain the Telegram token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        with process_lock(config_path().parent / "workfinder.lock"):
            config = initialize()
            asyncio.run(run_bot(config))
    except (ValueError, TelegramError) as exc:
        logger.error("Bot startup failed: %s", exc)
        raise SystemExit(1) from None
    except httpx.HTTPError:
        logger.error("Cannot connect to Telegram. Retry when the connection is available.")
        raise SystemExit(1) from None
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("WorkFinder stopped")
