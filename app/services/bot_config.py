from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import PROJECT_ROOT, settings
from app.models import CandidateProfile, CompanyPreference, CvVersion, IgnoreRule, Job, SavedSearch
from app.profiles.context import current_profile
from app.profiles.scope import validate_scope
from app.services.ignoring import recalculate_rule_ignores
from app.services.matching import score_job

PROFILE_FIELDS = {
    "name": str,
    "education": str,
    "skills": list,
    "technologies": list,
    "languages": list,
    "preferred_jobs": list,
    "preferred_industries": list,
    "preferred_locations": list,
    "available_hours": str,
    "desired_salary": str,
    "remote_preference": str,
}
SEARCH_FIELDS = {
    "name": str,
    "keywords": list,
    "negative_keywords": list,
    "locations": list,
    "sources": list,
    "minimum_score": int,
    "seniority": list,
    "employment_types": list,
    "companies": list,
    "enabled": bool,
}
COMPANY_FIELDS = {"company": str, "priority": str, "watched": bool, "notes": str}
RULE_FIELDS = {"rule_type": str, "value": str, "active": bool}
CV_FIELDS = {"name": str, "notes": str, "active": bool}
DEFAULTS = {
    "version": 1,
    "timezone": "Europe/Warsaw",
    "search_times": ["09:00", "17:00"],
    "reminder_time": "10:00",
    "minimum_score": 60,
    "page_size": 5,
    "sources": [],
}


def config_path() -> Path:
    if profile := current_profile():
        return profile.directory / "config.json"
    return PROJECT_ROOT / settings.config_path


def _validate_fields(value: object, fields: dict[str, type], label: str) -> None:
    if not isinstance(value, dict) or value.keys() - fields.keys():
        raise ValueError(f"{label}: nieznane pola lub nieprawidłowy obiekt")
    for key, item in value.items():
        expected = fields[key]
        if type(item) is not expected:
            raise ValueError(f"{label}.{key}: oczekiwano {expected.__name__}")
        if expected is list and any(not isinstance(part, str) for part in item):
            raise ValueError(f"{label}.{key}: wymagana lista tekstów")
        if expected is str and len(item) > 10000:
            raise ValueError(f"{label}.{key}: tekst jest zbyt długi")


def _clock(value: object) -> time:
    if not isinstance(value, str) or len(value) != 5:
        raise ValueError("Godziny podawaj jako HH:MM")
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Godziny podawaj jako HH:MM") from exc
    if parsed.strftime("%H:%M") != value:
        raise ValueError("Godziny podawaj jako HH:MM")
    return parsed


def validate_config(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("Konfiguracja musi być obiektem JSON")
    allowed = set(DEFAULTS) | {
        "profile",
        "searches",
        "companies",
        "ignore_rules",
        "cv_versions",
        "job_scope",
    }
    if raw.keys() - allowed:
        raise ValueError(f"Nieznane pola konfiguracji: {', '.join(sorted(raw.keys() - allowed))}")
    config = deepcopy(DEFAULTS | raw)
    if "job_scope" in config:
        config["job_scope"] = validate_scope(config["job_scope"])
    if type(config["version"]) is not int or config["version"] != 1:
        raise ValueError("Obsługiwana wersja konfiguracji: 1")
    try:
        ZoneInfo(config["timezone"])
    except (ZoneInfoNotFoundError, TypeError, ValueError) as exc:
        raise ValueError("Nieprawidłowa strefa czasowa") from exc
    for field, low, high in (("minimum_score", 0, 100), ("page_size", 1, 10)):
        if type(config[field]) is not int or not low <= config[field] <= high:
            raise ValueError(f"{field}: wymagana liczba od {low} do {high}")
    if not isinstance(config["search_times"], list):
        raise ValueError("search_times: wymagana lista godzin")
    for value in [*config["search_times"], config["reminder_time"]]:
        _clock(value)
    if not isinstance(config["sources"], list) or any(
        not isinstance(name, str) for name in config["sources"]
    ):
        raise ValueError("sources: wymagana lista nazw źródeł")
    if config["sources"]:
        from app.scrapers.registry import enabled_sources

        unknown = set(config["sources"]) - {source.name for source in enabled_sources()}
        if unknown:
            raise ValueError(f"Nieznane źródła: {', '.join(sorted(unknown))}")
    if "profile" in config:
        _validate_fields(config["profile"], PROFILE_FIELDS, "profile")
        if "job_scope" not in config and config["profile"].get(
            "preferred_locations", ["Kraków"]
        ) != ["Kraków"]:
            raise ValueError("Obsługiwana lokalizacja profilu: Kraków")
    for section, fields, identity in (
        ("searches", SEARCH_FIELDS, "name"),
        ("companies", COMPANY_FIELDS, "company"),
        ("ignore_rules", RULE_FIELDS, "value"),
        ("cv_versions", CV_FIELDS, "name"),
    ):
        if section not in config:
            continue
        rows = config[section]
        if not isinstance(rows, list):
            raise ValueError(f"{section}: wymagana lista obiektów")
        seen = set()
        for row in rows:
            _validate_fields(row, fields, section)
            if not row.get(identity, "").strip():
                raise ValueError(f"{section}: wymagane pole {identity}")
            key = (row.get("rule_type"), row[identity])
            if key in seen:
                raise ValueError(f"{section}: powtórzony wpis {row[identity]}")
            seen.add(key)
            if section == "searches":
                if not 0 <= row.get("minimum_score", 50) <= 100:
                    raise ValueError("searches.minimum_score: wymagana liczba od 0 do 100")
                if "job_scope" not in config and row.get("locations", ["Kraków"]) != ["Kraków"]:
                    raise ValueError("Obsługiwana lokalizacja wyszukiwania: Kraków")
            if section == "ignore_rules" and row.get("rule_type") not in {"keyword", "company"}:
                raise ValueError("ignore_rules.rule_type: keyword lub company")
    return config


def export_config(session: Session, path: Path | None = None) -> Path:
    path = path or config_path()
    raw = deepcopy(DEFAULTS)
    if profile_context := current_profile():
        raw["job_scope"] = deepcopy(profile_context.scope)
    profile = session.scalar(select(CandidateProfile).limit(1))
    if profile:
        raw["profile"] = {key: getattr(profile, key) for key in PROFILE_FIELDS}
    for section, model, fields in (
        ("searches", SavedSearch, SEARCH_FIELDS),
        ("companies", CompanyPreference, COMPANY_FIELDS),
        ("ignore_rules", IgnoreRule, RULE_FIELDS),
        ("cv_versions", CvVersion, CV_FIELDS),
    ):
        raw[section] = [
            {key: getattr(row, key) for key in fields}
            for row in session.scalars(select(model).order_by(model.id))
        ]
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation preserves the user's existing configuration.
    with path.open("x", encoding="utf-8") as handle:
        json.dump(raw, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return path


def load_config(path: Path | None = None) -> dict:
    try:
        with (path or config_path()).open(encoding="utf-8-sig") as handle:
            return validate_config(json.load(handle))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Błąd JSON w wierszu {exc.lineno}, kolumnie {exc.colno}") from exc


def apply_config(session: Session, config: dict) -> None:
    config = validate_config(config)
    if "job_scope" in config and current_profile() is None:
        raise ValueError("JSON z job_scope wymaga osobnego profilu; użyj profile-create")
    if "profile" in config:
        profile = session.scalar(select(CandidateProfile).limit(1)) or CandidateProfile()
        session.add(profile)
        for key, value in config["profile"].items():
            setattr(profile, key, value)
    # Omitted sections retain legacy data. Present lists define active configuration.
    for section, model, identity, active in (
        ("searches", SavedSearch, "name", "enabled"),
        ("companies", CompanyPreference, "company", "watched"),
        ("ignore_rules", IgnoreRule, "value", "active"),
        ("cv_versions", CvVersion, "name", "active"),
    ):
        if section not in config:
            continue
        existing = {
            (getattr(row, "rule_type", None), getattr(row, identity)): row
            for row in session.scalars(select(model))
        }
        for row in existing.values():
            setattr(row, active, False)
        for fields in config[section]:
            key = (fields.get("rule_type"), fields[identity])
            row = existing.get(key)
            if row is None:
                row = model()
                session.add(row)
            setattr(row, active, fields.get(active, True))
            for name, value in fields.items():
                setattr(row, name, value)
    session.flush()
    profile = session.scalar(select(CandidateProfile).limit(1))
    for job in session.scalars(select(Job)):
        result = score_job(job, profile)
        job.match_score = result.score
        job.match_explanation = result.explanation
        job.skills = result.skills
        job.missing_skills = result.missing_skills
        job.categories = result.categories
    recalculate_rule_ignores(session)


def next_search(config: dict, now: datetime) -> datetime | None:
    zone = ZoneInfo(config["timezone"])
    local = now.replace(tzinfo=UTC).astimezone(zone)
    candidates = []
    for offset in (0, 1):
        day = local.date() + timedelta(days=offset)
        for value in config["search_times"]:
            candidate = datetime.combine(day, _clock(value), zone).astimezone(UTC)
            if candidate > now.replace(tzinfo=UTC):
                candidates.append(candidate.replace(tzinfo=None))
    return min(candidates, default=None)
