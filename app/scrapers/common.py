from __future__ import annotations

import html
import re
from datetime import UTC, date, datetime

SENIOR_TITLE_PATTERN = re.compile(
    r"\b(?:senior|sr\.?|lead|principal|manager|director|head|chief|supervisor|architect|"
    r"vice[ -]president|vp|starszy|starsza|kierownik|kierowniczka|dyrektor|dyrektorka|"
    r"lider|liderka|architekt|architektka)\b",
    re.IGNORECASE,
)

INTERNSHIP_KEYWORDS = [
    "intern",
    "internship",
    "trainee",
    "apprentice",
    "placement",
    "staż",
    "staz",
    "praktyk",
    "working student",
    "student program",
]
INTERNSHIP_PATTERNS = {
    "Internship": (
        r"\b(?:intern(?:s|ship(?:s)?)?|sta[żz](?:e|ysta|ystka|ystki|ystów|ystow|"
        r"owy|owa|owe|owego|owym|owych)?|praktyk(?:a|i|ant\w*|anck\w*))\b"
    ),
    "Trainee": r"\btrainee(?:s|ship(?:s)?)?\b",
    "Apprenticeship": r"\bapprentice(?:s|ship(?:s)?)?\b",
    "Student placement": (
        r"\b(?:working[ -]student|student[ -](?:placement|program(?:me)?|internship)s?|"
        r"industrial[ -]placement|placement[ -](?:student|year))\b"
    ),
}
INTERNSHIP_PATTERN = "|".join(f"(?:{pattern})" for pattern in INTERNSHIP_PATTERNS.values())


def internship_label(title: str, employment_type: str = "", seniority: str = "") -> str | None:
    """Require evidence in role metadata; incidental description mentions do not qualify."""
    if is_senior_role(title):
        return None
    for label, pattern in INTERNSHIP_PATTERNS.items():
        if any(
            re.search(pattern, value or "", re.IGNORECASE)
            for value in (title, employment_type, seniority)
        ):
            return label
    return None


def is_internship_role(title: str, employment_type: str = "", seniority: str = "") -> bool:
    return internship_label(title, employment_type, seniority) is not None


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
            return datetime.fromtimestamp(value, UTC).date()
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
