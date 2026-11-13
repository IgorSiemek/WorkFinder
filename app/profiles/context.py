from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ProfileContext:
    key: str
    directory: Path
    sessions: Any
    engine: Any
    scope: dict


_current: ContextVar[ProfileContext | None] = ContextVar("workfinder_profile", default=None)


def current_profile() -> ProfileContext | None:
    return _current.get()


@contextmanager
def activate_profile(profile: ProfileContext):
    token = _current.set(profile)
    try:
        yield profile
    finally:
        _current.reset(token)
