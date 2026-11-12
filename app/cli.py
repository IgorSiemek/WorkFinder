from __future__ import annotations

import argparse
import asyncio

import httpx
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.database import SessionLocal, init_database
from app.models import BotState, utcnow
from app.services.backups import automatic_backup, backup_database
from app.services.bot_config import config_path, export_config, load_config
from app.services.excel_import import import_excel_jobs
from app.services.progress import retry_excel_sync
from app.services.publication_dates import refresh_posting_dates
from app.services.search import run_search
from app.services.seed import remove_demo, seed_demo


def main() -> None:
    parser = argparse.ArgumentParser(description="WorkFinder command line tools")
    parser.add_argument(
        "command",
        choices=[
            "bot",
            "export-config",
            "check-config",
            "telegram-id",
            "healthcheck",
            "init",
            "search",
            "import-excel",
            "seed-demo",
            "remove-demo",
            "backup",
            "retry-excel",
            "refresh-dates",
        ],
    )
    args = parser.parse_args()
    if args.command == "healthcheck":
        from datetime import datetime, timedelta

        try:
            with SessionLocal() as session:
                polls = session.scalars(
                    select(BotState).where(
                        BotState.key.endswith(f":{settings.telegram_user_id}:last_poll"),
                    )
                ).all()
                for poll in polls:
                    build = session.get(BotState, poll.key.removesuffix("last_poll") + "build_ref")
                    if (
                        poll.value
                        and build
                        and build.value == settings.build_ref
                        and (datetime.fromisoformat(poll.value) >= utcnow() - timedelta(seconds=90))
                    ):
                        return
        except (SQLAlchemyError, ValueError):
            pass
        raise SystemExit(1)
    if args.command == "telegram-id":
        from app.telegram.client import TelegramClient, TelegramError

        if not settings.telegram_token:
            parser.error("Set TELEGRAM_BOT_TOKEN in .env first.")

        async def identify() -> list[int]:
            client = TelegramClient(settings.telegram_token)
            try:
                updates = await client.call(
                    "getUpdates", timeout=25, allowed_updates=["message", "callback_query"]
                )
                identifiers = set()
                for update in updates:
                    message = update.get("message", {})
                    chat = message.get("chat", {})
                    if chat.get("type") == "private":
                        identifiers.add(chat["id"])
                return sorted(identifiers)
            finally:
                await client.close()

        try:
            identifiers = asyncio.run(identify())
        except (httpx.HTTPError, TelegramError):
            parser.error("Cannot read Telegram updates. Check token, network and webhook settings.")
        for identifier in identifiers:
            print(f"Private chat ID: {identifier}")
        if not identifiers:
            print(
                "Send /start to the bot and run telegram-id again. Stop other bot instances first."
            )
        return
    if args.command == "bot":
        from app.telegram.runtime import main as bot_main

        bot_main()
        return
    if args.command == "check-config":
        load_config()
        print("Configuration valid.")
        return
    from app.telegram.runtime import initialize, process_lock

    if args.command in {"init", "search"}:
        with process_lock(config_path().parent / "workfinder.lock"):
            config = initialize()
            if args.command == "init":
                print(f"Database initialized. Configuration: {config_path()}")
                return
            with SessionLocal() as session:
                result = run_search(session, source_names=config["sources"])
                print(
                    f"Search {result.status}: {result.jobs_found} found, "
                    f"{result.new_jobs} new, {result.errors} errors."
                )
                return
    automatic_backup()
    init_database()
    with SessionLocal() as session:
        if args.command == "export-config":
            path = export_config(session, config_path().with_name("config.export.json"))
            print(f"Configuration exported: {path}")
        elif args.command == "backup":
            print(f"Backup: {backup_database(force=True)}")
        elif args.command == "retry-excel":
            done, pending = retry_excel_sync(session)
            print(f"Excel sync: {done} completed, {pending} pending.")
        elif args.command == "refresh-dates":
            counts = refresh_posting_dates(session, force=True)
            print(
                f"Posting dates: {counts['verified']} verified, {counts['relative']} estimated, "
                f"{counts['unknown']} unavailable."
            )
        elif args.command == "seed-demo":
            print(f"Added {seed_demo(session)} demo jobs.")
        elif args.command == "remove-demo":
            print(f"Removed {remove_demo(session)} demo jobs.")
        else:
            if not settings.excel_jobs_path:
                parser.error("Set EXCEL_JOBS_PATH in .env before importing.")
            result = import_excel_jobs(session, settings.excel_jobs_path)
            print(
                f"Excel import: {result.rows_read} rows, {result.new_jobs} new, "
                f"{result.updated_jobs} updated, {result.applications_created} applications, "
                f"{result.skipped_rows} skipped."
            )
            for warning in result.warnings:
                print(f"Warning: {warning}")


if __name__ == "__main__":
    main()
