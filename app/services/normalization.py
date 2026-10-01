from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

COMPANY_SUFFIXES = re.compile(
    r"\b(sp\.?\s*z\.?\s*o\.?\s*o\.?|s\.?a\.?|ltd\.?|limited|inc\.?|llc|plc|group)\b",
    re.IGNORECASE,
)
TITLE_NOISE = re.compile(r"\b(m/f/d|f/m/d|all genders)\b", re.IGNORECASE)
TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "ref"}


def fold_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", ascii_text).strip()


def normalize_company(value: str) -> str:
    return re.sub(r"\s+", " ", COMPANY_SUFFIXES.sub("", fold_text(value))).strip()


def normalize_title(value: str) -> str:
    value = TITLE_NOISE.sub("", value)
    tokens = fold_text(value).split()
    aliases = {
        "internship": "intern",
        "trainee": "intern",
        "jr": "junior",
        "financial": "finance",
    }
    normalized = [aliases.get(token, token) for token in tokens]
    return " ".join(sorted(normalized))


def normalize_location(value: str) -> str:
    normalized = fold_text(value)
    normalized = normalized.replace("cracow", "krakow")
    return re.sub(r"\b(poland|polska)\b", "", normalized).strip()


def canonicalize_url(value: str) -> str:
    parts = urlsplit(value.strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("Only valid http(s) URLs are accepted")
    query = [
        (key, val) for key, val in parse_qsl(parts.query) if key.casefold() not in TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (parts.scheme.casefold(), parts.netloc.casefold(), path, urlencode(query), "")
    )


def description_fingerprint(value: str) -> str:
    words = fold_text(value).split()
    stable = " ".join(words[:500])
    return hashlib.sha256(stable.encode()).hexdigest()


def job_hash(company: str, title: str, location: str, description: str) -> str:
    key = "|".join(
        [
            normalize_company(company),
            normalize_title(title),
            normalize_location(location),
            description_fingerprint(description),
        ]
    )
    return hashlib.sha256(key.encode()).hexdigest()
