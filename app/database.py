from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import PROJECT_ROOT, settings


class Base(DeclarativeBase):
    pass


def _database_url() -> str:
    url = settings.database_url
    if url.startswith("sqlite:///./"):
        return f"sqlite:///{(PROJECT_ROOT / url.removeprefix('sqlite:///./')).as_posix()}"
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        relative = url.removeprefix("sqlite:///")
        return f"sqlite:///{(PROJECT_ROOT / relative).as_posix()}"
    return url


database_url = _database_url()
if database_url.startswith("sqlite:///"):
    Path(database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    database_url,
    connect_args={"check_same_thread": False, "timeout": 30}
    if database_url.startswith("sqlite")
    else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@event.listens_for(engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
    if database_url.startswith("sqlite"):
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.close()


def init_database() -> None:
    from alembic.config import Config

    from alembic import command

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
