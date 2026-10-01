from __future__ import annotations

import argparse

from app.config import settings
from app.database import SessionLocal, init_database
from app.services.excel_import import import_excel_jobs
from app.services.search import run_search
from app.services.seed import remove_demo, seed_defaults, seed_demo


def main() -> None:
    parser = argparse.ArgumentParser(description="WorkFinder command line tools")
    parser.add_argument(
        "command", choices=["init", "search", "import-excel", "seed-demo", "remove-demo"]
    )
    args = parser.parse_args()
    init_database()
    with SessionLocal() as session:
        seed_defaults(session)
        if args.command == "init":
            print("Database initialized.")
        elif args.command == "search":
            result = run_search(session)
            print(
                f"Search {result.status}: {result.jobs_found} found, "
                f"{result.new_jobs} new, {result.errors} provider errors."
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
