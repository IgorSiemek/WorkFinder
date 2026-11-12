from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from types import SimpleNamespace

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app import database
from app.database import Base
from app.models import Job
from app.services import backups


def test_online_backup_contains_committed_data_and_is_idempotent(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    bind = create_engine(f"sqlite:///{(tmp_path / 'live.db').as_posix()}")
    monkeypatch.setattr(backups, "settings", SimpleNamespace(backup_dir=str(tmp_path / "backups")))
    try:
        with bind.begin() as connection:
            connection.execute(text("CREATE TABLE progress (status TEXT)"))
            connection.execute(text("INSERT INTO progress VALUES ('Applied')"))
        first = backups.backup_database(bind)
        assert first == backups.backup_database(bind)
        with closing(sqlite3.connect(first)) as saved:
            assert saved.execute("SELECT status FROM progress").fetchone()[0] == "Applied"
            assert saved.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        forced = backups.backup_database(bind, force=True)
        assert forced != first and forced.is_file()
        assert not list(first.parent.glob("*.tmp"))
    finally:
        bind.dispose()


def test_legacy_database_upgrade_preserves_data_and_is_repeatable(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    url = f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}"
    bind = create_engine(url)
    old_tables = [
        table
        for table in Base.metadata.sorted_tables
        if table.name
        not in {
            "excel_sync_tasks",
            "search_observations",
            "bot_state",
            "bot_messages",
            "application_drafts",
        }
    ]
    Base.metadata.create_all(bind, tables=old_tables)
    with Session(bind) as session:
        session.add(
            Job(
                source="Excel",
                title="Finance Intern",
                company="Example",
                location="Kraków",
                original_url="https://example.com/legacy-date",
                canonical_url="https://example.com/legacy-date",
                job_hash="legacy-date",
                date_posted=date(2026, 9, 10),
                notes="Existing note",
            )
        )
        session.commit()
    with bind.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO company_preferences "
                "(company, priority, watched, notes, created_at, updated_at) "
                "VALUES ('Existing Company', 'High priority', 1, '', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
    monkeypatch.setattr(database, "engine", bind)
    monkeypatch.setattr(database, "database_url", url)
    try:
        database.init_database()
        database.init_database()
        assert {"excel_sync_tasks", "search_observations", "alembic_version"} <= set(
            inspect(bind).get_table_names()
        )
        with bind.connect() as connection:
            assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
            assert (
                connection.scalar(text("SELECT company FROM company_preferences"))
                == "Existing Company"
            )
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "20261112_0005"
            )
            assert connection.scalar(text("SELECT date_posted FROM jobs")) is None
            assert "Existing note" in connection.scalar(text("SELECT notes FROM jobs"))
            assert "2026-09-10" in connection.scalar(text("SELECT notes FROM jobs"))
    finally:
        bind.dispose()
