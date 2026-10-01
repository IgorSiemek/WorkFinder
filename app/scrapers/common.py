from __future__ import annotations

import html
import re
from datetime import date, datetime

SENIOR_TITLE_PATTERN = re.compile(
    r"\b(?:senior|sr\.?|lead|principal|manager|director|head|chief|supervisor|architect|"
    r"vice[ -]president|vp|starszy|starsza|kierownik|kierowniczka|dyrektor|dyrektorka|"
    r"lider|liderka|architekt|architektka)\b",
    re.IGNORECASE,
)


def plain_text(value: str | None) -> str:
    if not value:
        return ""
    value = re.sub(r"<script.*?</script>|<style.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def parse_date(value: str | int | None) -> date | None:
    if value is None:
        return None
    try:
        if isinstance(value, int):
            return datetime.fromtimestamp(value).date()
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except (ValueError, OSError):
        return None


def relevant_to_krakow(location: str, description: str, remote: bool) -> bool:
    del description
    location_text = location.casefold()
    krakow = any(term in location_text for term in ("kraków", "krakow", "cracow"))
    return krakow and not remote


def is_senior_role(title: str) -> bool:
    return bool(SENIOR_TITLE_PATTERN.search(title))
