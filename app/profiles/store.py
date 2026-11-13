from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.profiles.context import ProfileContext, activate_profile
from app.services.bot_config import PROFILE_FIELDS, apply_config, load_config, validate_config


def validate_personal_config(raw: dict) -> dict:
    from app.scrapers.registry import _company_sources
    from app.scrapers.workday import WorkdaySource

    config = validate_config(raw)
    if not config.get("profile", {}).get("name", "").strip() or "job_scope" not in config:
        raise ValueError("Nowy profil wymaga profile.name i jawnego job_scope")
    supported = {
        "Arbeitnow",
        *[source.name for source in _company_sources() if isinstance(source, WorkdaySource)],
    }
    if not config["sources"] or set(config["sources"]) - supported:
        raise ValueError(
            "Nowy profil wymaga jawnych źródeł Workday lub Arbeitnow: "
            + ", ".join(sorted(supported))
        )
    # Never seed the original student's preferences into a new person's workspace.
    config["profile"] = {
        key: ([] if kind is list else "") for key, kind in PROFILE_FIELDS.items()
    } | config["profile"]
    for field in ("searches", "companies", "ignore_rules", "cv_versions"):
        config.setdefault(field, [])
    return config


def profile_context(key: str, directory: Path, scope: dict) -> ProfileContext:
    bind = create_engine(
        f"sqlite:///{(directory / 'workfinder.db').as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(bind, "connect")
    def pragmas(connection, _):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.close()

    return ProfileContext(
        key,
        directory,
        sessionmaker(bind=bind, autoflush=False, expire_on_commit=False),
        bind,
        scope,
    )


class ProfileStore:
    """Private registry of new workspaces. Never opens or migrates the legacy database."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.path = self.root / "registry.sqlite3"

    def exists(self) -> bool:
        return self.path.is_file()

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(self.connect()) as connection, connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS profiles (
                    profile_key TEXT PRIMARY KEY, name TEXT NOT NULL,
                    user_id INTEGER UNIQUE, enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS invites (
                    digest TEXT PRIMARY KEY,
                    profile_key TEXT NOT NULL REFERENCES profiles(profile_key),
                    expires_at REAL NOT NULL, used_by INTEGER
                );
            """)
        self.path.chmod(0o600)

    def directory(self, key: str) -> Path:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", key) or key in {"default", "legacy"}:
            raise ValueError("Nazwa profilu: małe litery, cyfry, _ lub -, maksymalnie 48 znaków")
        path = (self.root / key).resolve()
        if path.parent != self.root:
            raise ValueError("Nieprawidłowa ścieżka profilu")
        return path

    def create(self, key: str, raw: dict) -> str:
        config = validate_personal_config(raw)
        directory = self.directory(key)
        self.initialize()
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM profiles WHERE profile_key=?", (key,)).fetchone():
                raise ValueError("Profil już istnieje; jego danych nie nadpisano")
            directory.mkdir(mode=0o700)  # Exclusive creation also rejects abandoned directories.
            context = profile_context(key, directory, config["job_scope"])
            try:
                from app.database import init_database

                with activate_profile(context):
                    init_database()
                    with context.sessions() as session:
                        apply_config(session, config)
                        session.commit()
                path = directory / "config.json"
                with os.fdopen(
                    os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                    "w",
                    encoding="utf-8",
                ) as handle:
                    json.dump(config, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                (directory / "workfinder.db").chmod(0o600)
                connection.execute(
                    "INSERT INTO profiles(profile_key,name) VALUES (?,?)",
                    (key, config["profile"]["name"]),
                )
            finally:
                context.engine.dispose()
        return self.invite(key)

    def invite(self, key: str) -> str:
        self.directory(key)
        if not self.exists():
            raise ValueError("Profil nie istnieje")
        token = secrets.token_urlsafe(24)
        with closing(self.connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM profiles WHERE profile_key=?", (key,)
            ).fetchone()
            if not row or not row["enabled"] or row["user_id"] is not None:
                raise ValueError("Profil nie istnieje, jest wyłączony albo ma już przypisane konto")
            connection.execute(
                "UPDATE invites SET expires_at=0 WHERE profile_key=? AND used_by IS NULL", (key,)
            )
            connection.execute(
                "INSERT INTO invites(digest,profile_key,expires_at) VALUES (?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), key, time.time() + 86400),
            )
        return token

    def claim(self, token: str, user_id: int, *, reserved: set[int]) -> dict | None:
        if type(user_id) is not int or user_id <= 0 or user_id in reserved or not self.exists():
            return None
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM profiles WHERE user_id=?", (user_id,)).fetchone():
                return None
            row = connection.execute(
                "SELECT p.* FROM invites i JOIN profiles p USING(profile_key) "
                "WHERE i.digest=? AND i.used_by IS NULL AND i.expires_at>? "
                "AND p.enabled=1 AND p.user_id IS NULL",
                (hashlib.sha256(token.encode()).hexdigest(), time.time()),
            ).fetchone()
            if not row:
                return None
            connection.execute(
                "UPDATE profiles SET user_id=? WHERE profile_key=?", (user_id, row["profile_key"])
            )
            connection.execute(
                "UPDATE invites SET used_by=? WHERE digest=?",
                (user_id, hashlib.sha256(token.encode()).hexdigest()),
            )
            return dict(row) | {"user_id": user_id}

    def for_user(self, user_id: int) -> dict | None:
        if not self.exists():
            return None
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM profiles WHERE user_id=?", (user_id,)
            ).fetchone()
            return dict(row) if row else None

    def profiles(self) -> list[dict]:
        if not self.exists():
            return []
        with closing(self.connect()) as connection:
            return [
                dict(row)
                for row in connection.execute("SELECT * FROM profiles ORDER BY profile_key")
            ]

    def open(self, key: str) -> tuple[ProfileContext, dict]:
        directory = self.directory(key)
        if not (directory / "workfinder.db").is_file():
            raise ValueError("Brak bazy danych profilu")
        config = validate_personal_config(load_config(directory / "config.json"))
        return profile_context(key, directory, config["job_scope"]), config
