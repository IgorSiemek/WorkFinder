from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.config import PROJECT_ROOT, settings
from app.database import engine
from app.profiles.context import current_profile

logger = logging.getLogger(__name__)


def backup_directory() -> Path:
    if profile := current_profile():
        return profile.directory / "backups"
    path = Path(settings.backup_dir).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def list_backups() -> list[Path]:
    return sorted(backup_directory().glob("workfinder-*.sqlite3"), reverse=True)


def backup_database(bind: Engine | None = None, *, force: bool = False) -> Path | None:
    """Create a consistent online SQLite snapshot, once per UTC day by default."""
    bind = bind or (current_profile().engine if current_profile() else engine)
    if bind.dialect.name != "sqlite" or not bind.url.database or bind.url.database == ":memory:":
        return None
    directory = backup_directory()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f" if force else "%Y%m%d")
    destination = directory / f"workfinder-{stamp}.sqlite3"
    if destination.exists():
        destination.chmod(0o600)
        return destination
    temporary = directory / f".{uuid4().hex}.tmp"
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(descriptor)
        raw = bind.raw_connection()
        try:
            with closing(sqlite3.connect(temporary)) as target:
                raw.driver_connection.backup(target, pages=256)
                if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise OSError("Database backup failed its integrity check")
        finally:
            raw.close()
        os.replace(temporary, destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def automatic_backup(bind: Engine | None = None) -> None:
    try:
        backup_database(bind)
    except (OSError, sqlite3.Error, SQLAlchemyError):
        # A failed backup must never undo a successfully saved application.
        logger.exception("Automatic backup failed; use /backup or the backup CLI command to retry")
