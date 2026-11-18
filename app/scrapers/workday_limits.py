from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from threading import Lock
from urllib.parse import urlsplit

import httpx


class WorkdayRateLimited(httpx.HTTPError):
    def __init__(self, retry_at: datetime) -> None:
        super().__init__(f"Workday HTTP 429. Kolejna próba od {retry_at:%Y-%m-%d %H:%M:%S} UTC.")
        self.retry_at = retry_at


@dataclass
class _HostAccess:
    lock: Lock = field(default_factory=Lock)
    retry_at: datetime | None = None


_hosts: defaultdict[str | None, _HostAccess] = defaultdict(_HostAccess)
_hosts_lock = Lock()
_default_cooldown = timedelta(hours=1)


def _retry_at(value: str, now: datetime) -> datetime:
    value = value.strip()
    try:
        if value.isdecimal():
            return now + timedelta(seconds=int(value))
        if value:
            date = parsedate_to_datetime(value)
            return max(now, date.replace(tzinfo=date.tzinfo or UTC).astimezone(UTC))
    except (ValueError, OverflowError):
        pass
    return now + _default_cooldown


@contextmanager
def workday_access(url: str) -> Iterator[None]:
    with _hosts_lock:
        access = _hosts[urlsplit(url).hostname]
    with access.lock:
        if access.retry_at is not None and datetime.now(UTC) < access.retry_at:
            raise WorkdayRateLimited(access.retry_at)
        try:
            yield
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 429:
                raise
            access.retry_at = _retry_at(
                exc.response.headers.get("Retry-After", ""), datetime.now(UTC)
            )
            raise WorkdayRateLimited(access.retry_at) from exc
