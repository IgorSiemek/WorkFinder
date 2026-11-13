from __future__ import annotations

import re
import unicodedata

from app.profiles.context import current_profile
from app.scrapers.common import INTERNSHIP_PATTERN, is_internship_role, relevant_to_krakow

SCOPE_DEFAULTS = {
    "role_type": "internships",
    "locations": ["Kraków"],
    "remote_allowed": False,
    "titles": [],
    "excluded_titles": [],
}


def validate_scope(raw: object) -> dict:
    if not isinstance(raw, dict) or raw.keys() - SCOPE_DEFAULTS.keys():
        raise ValueError("job_scope: nieprawidłowe lub nieznane pola")
    scope = SCOPE_DEFAULTS | raw
    if not isinstance(scope["role_type"], str) or scope["role_type"] not in {
        "internships",
        "regular",
        "all",
    }:
        raise ValueError("job_scope.role_type: internships, regular lub all")
    if type(scope["remote_allowed"]) is not bool:
        raise ValueError("job_scope.remote_allowed: wymagana wartość logiczna")
    for key in ("locations", "titles", "excluded_titles"):
        values = scope[key]
        if (
            not isinstance(values, list)
            or len(values) > 20
            or any(
                not isinstance(value, str) or not value.strip() or len(value) > 200
                for value in values
            )
        ):
            raise ValueError(f"job_scope.{key}: wymagana lista tekstów (maksymalnie 20)")
    if not scope["locations"] and not scope["remote_allowed"]:
        raise ValueError("job_scope: podaj lokalizację lub dopuść pracę zdalną")
    return scope


def active_scope() -> dict | None:
    profile = current_profile()
    return profile.scope if profile else None


def role_matches(title: str, employment_type: str = "", seniority: str = "") -> bool:
    scope = active_scope()
    intern = is_internship_role(title, employment_type, seniority)
    if scope is None:
        return intern
    if scope["role_type"] == "internships" and not intern:
        return False
    if scope["role_type"] == "regular" and re.search(
        INTERNSHIP_PATTERN, f"{title} {employment_type} {seniority}", re.I
    ):
        return False
    text = title.casefold()
    return (
        not scope["titles"] or any(term.casefold() in text for term in scope["titles"])
    ) and not any(term.casefold() in text for term in scope["excluded_titles"])


def normalized_location(value: str) -> str:
    text = (
        "".join(
            char
            for char in unicodedata.normalize("NFKD", value.casefold())
            if not unicodedata.combining(char)
        )
        .replace("cracow", "krakow")
        .replace("ł", "l")
    )
    return text.replace("warszawa", "warsaw")


def location_matches(location: str, description: str = "", remote: bool = False) -> bool:
    scope = active_scope()
    if scope is None:
        return relevant_to_krakow(location, description, remote)
    if remote:
        return scope["remote_allowed"]
    text = normalized_location(location)
    return any(normalized_location(city) in text for city in scope["locations"])


def job_filter():
    from sqlalchemy import and_, or_, true

    from app.models import Job

    scope = active_scope()
    if scope is None:
        return Job.is_internship
    role = true()
    if scope["role_type"] == "internships":
        role = Job.is_internship
    elif scope["role_type"] == "regular":
        role = ~or_(
            *[
                field.regexp_match(f"(?i){INTERNSHIP_PATTERN}")
                for field in (
                    Job.title,
                    Job.employment_type,
                    Job.seniority,
                )
            ]
        )
    filters = [role]
    for key, negate in (("titles", False), ("excluded_titles", True)):
        if scope[key]:
            pattern = "(?i)(?:" + "|".join(re.escape(term) for term in scope[key]) + ")"
            condition = Job.title.regexp_match(pattern)
            filters.append(~condition if negate else condition)
    aliases = {"krakow": ["Kraków", "Krakow", "Cracow"], "warsaw": ["Warszawa", "Warsaw"]}
    terms = []
    for city in scope["locations"]:
        terms.extend(aliases.get(normalized_location(city), [city, normalized_location(city)]))
    location = (
        Job.location.regexp_match("(?i)(?:" + "|".join(re.escape(term) for term in terms) + ")")
        if terms
        else ~true()
    )
    remote = Job.remote_type == "Remote"
    filters.append(or_(location, remote) if scope["remote_allowed"] else and_(location, ~remote))
    return and_(*filters)
