from __future__ import annotations

import re
from copy import deepcopy
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ApplicationDraft
from app.scrapers.registry import enabled_sources
from app.scrapers.workday import WorkdaySource
from app.services.jobs import get_job
from app.services.progress import change_status

ACTIVE = {"queued", "working", "input", "review", "submitting", "cancel_requested"}
PROFILE_FIELDS = {
    "first_name": "Imię",
    "last_name": "Nazwisko",
    "email": "E-mail",
    "phone": "Telefon z numerem kierunkowym",
    "address": "Ulica i numer domu",
    "city": "Miasto zamieszkania",
    "postal_code": "Kod pocztowy",
    "country": "Kraj zamieszkania po angielsku, np. Poland",
    "linkedin": "LinkedIn (opcjonalnie, znak - pomija)",
}
REQUIRED_PROFILE = {"first_name", "last_name", "email", "phone"}
MAX_CV_BYTES = 10 * 1024 * 1024


def question_key(label: str) -> str:
    return re.sub(r"\s+", " ", label.replace("*", "")).strip().casefold()


def workday_host(job) -> str | None:  # noqa: ANN001
    try:
        parsed = urlsplit(job.original_url)
        port = parsed.port
    except ValueError:
        return None
    for source in enabled_sources():
        if (
            isinstance(source, WorkdaySource)
            and job.company == source.config.company
            and parsed.scheme == "https"
            and parsed.hostname == source.config.host
            and port in {None, 443}
            and parsed.username is None
            and "/job/" in parsed.path
        ):
            return parsed.hostname
    return None


def shared_profile(controller, session: Session) -> dict:  # noqa: ANN001
    return controller.state(session, "applicant", {}, scope=controller.admin_scope)


def save_profile_value(controller, session: Session, field: str, value: str) -> None:  # noqa: ANN001
    if field not in PROFILE_FIELDS or not value.strip() or len(value) > 500:
        raise ValueError("Podaj wartość o długości 1–500 znaków.")
    value = "" if value.strip() == "-" else value.strip()
    if field in REQUIRED_PROFILE and not value:
        raise ValueError("To pole jest wymagane.")
    if field == "email" and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise ValueError("Podaj poprawny adres e-mail.")
    profile = dict(shared_profile(controller, session))
    profile[field] = value
    controller.set_state(session, "applicant", profile, scope=controller.admin_scope)


def profile_message(controller, session: Session) -> dict:  # noqa: ANN001
    profile = shared_profile(controller, session)
    lines = ["<b>Dane do aplikowania</b>"]
    for key, label in PROFILE_FIELDS.items():
        lines.append(escape(label) + " · " + escape(profile.get(key) or "brak"))
    cv = controller.state(session, "application_cv", {}, scope=controller.admin_scope)
    lines.append("CV · " + escape(cv.get("name") or "brak pliku"))
    lines.append("\n/setup — uzupełnij dane krok po kroku. /uploadcv — prześlij CV w PDF.")
    lines.append("/applicant pole wartość — zmień pole, np. /applicant email anna@example.com")
    return {"text": "\n".join(lines)}


def setup_next(controller, session: Session, field: str | None = None) -> dict:  # noqa: ANN001
    fields = list(PROFILE_FIELDS)
    index = fields.index(field) + 1 if field else 0
    if index == len(fields):
        controller.set_state(session, "setup_field", None)
        return profile_message(controller, session)
    key = fields[index]
    controller.set_state(session, "setup_field", key)
    current = shared_profile(controller, session).get(key)
    text = PROFILE_FIELDS[key] + ("\nObecnie · " + current if current else "")
    return {
        "text": escape(text)
        + "\nWyślij odpowiedź w kolejnej wiadomości. /cancel kończy konfigurację."
    }


def prepare(controller, session: Session, job_id: int) -> ApplicationDraft:  # noqa: ANN001
    job = get_job(session, job_id)
    if not workday_host(job):
        raise ValueError(
            "Automatyczny formularz obsługuje teraz Motorola, BBH, Aptiv i State Street."
        )
    if not job.is_active or job.ignored:
        raise ValueError("Wybierz aktywną, widoczną ofertę.")
    if job.application and job.application.status not in {"Saved", "Preparing"}:
        raise ValueError("Ta oferta ma już zapisany status aplikacji. Sprawdź jej historię.")
    existing = session.scalar(select(ApplicationDraft).where(ApplicationDraft.job_id == job_id))
    if existing and existing.status in ACTIVE:
        if existing.user_id != controller.owner_id:
            raise ValueError("Drugie konto przygotowuje tę aplikację.")
        return existing
    if existing and existing.status in {"uncertain", "submitted"}:
        raise ValueError("Sprawdź zgłoszenie na stronie pracodawcy przed kolejną próbą.")
    if session.scalar(select(ApplicationDraft.id).where(ApplicationDraft.status.in_(ACTIVE))):
        raise ValueError("Otwarty jest już jeden formularz. Dokończ go lub anuluj przez /form.")
    profile = shared_profile(controller, session)
    missing = sorted(REQUIRED_PROFILE - {key for key, value in profile.items() if value})
    if missing:
        raise ValueError("Uzupełnij /setup. Brak pól · " + ", ".join(missing))
    cv = controller.state(session, "application_cv", {}, scope=controller.admin_scope)
    if not cv.get("path") or not Path(cv["path"]).is_file():
        raise ValueError("Prześlij najpierw CV w PDF przez /uploadcv.")
    draft = existing or ApplicationDraft(job=job, user_id=controller.owner_id)
    draft.user_id = controller.owner_id
    draft.status = "queued"
    draft.revision = (draft.revision or 0) + 1
    draft.profile = deepcopy(profile)
    bank = controller.state(session, "applicant_answers", {}, scope=controller.admin_scope)
    draft.profile["answers"] = deepcopy(bank.get(workday_host(job), {}))
    draft.cv = deepcopy(cv)
    draft.snapshot, draft.answers, draft.action = {}, {}, {}
    draft.error, draft.receipt = None, None
    session.add(draft)
    change_status(session, job, "Preparing", today=controller.today())
    session.flush()
    return draft


def owned_draft(controller, session: Session, draft_id: int | None = None) -> ApplicationDraft:  # noqa: ANN001
    query = select(ApplicationDraft).where(ApplicationDraft.user_id == controller.owner_id)
    if draft_id:
        query = query.where(ApplicationDraft.id == draft_id)
    draft = session.scalar(query.order_by(ApplicationDraft.updated_at.desc()).limit(1))
    if draft is None:
        raise ValueError("Brak formularza. Użyj przycisku Przygotuj aplikację przy ofercie.")
    return draft


def request_action(
    controller, session: Session, draft_id: int, revision: int, kind: str, **values
) -> ApplicationDraft:  # noqa: ANN001
    draft = owned_draft(controller, session, draft_id)
    if revision != draft.revision:
        raise ValueError("Ten widok formularza wygasł. Otwórz /form.")
    if kind == "cancel":
        if draft.status not in ACTIVE - {"submitting"}:
            raise ValueError("Wysyłanie już trwa lub formularz został zamknięty. Sprawdź /form.")
        draft.status = "cancel_requested"
        draft.action = {}
        return draft
    if draft.status not in {"input", "review"} or draft.action:
        raise ValueError("Poczekaj na wynik poprzedniego działania. Stan znajdziesz pod /form.")
    if kind not in {
        "answer",
        "next",
        "back",
        "openaccount",
        "opensignin",
        "send",
        "options",
        "refresh",
        "uploadcv",
    }:
        raise ValueError("Nieprawidłowe działanie.")
    if kind == "send" and draft.status != "review":
        raise ValueError("Najpierw sprawdź podsumowanie aplikacji.")
    if kind == "next" and draft.status == "review":
        raise ValueError("Formularz oczekuje na zatwierdzenie przyciskiem Wyślij aplikację.")
    if kind in {"answer", "options", "uploadcv"}:
        fields = draft.snapshot.get("fields", [])
        field = next((item for item in fields if item["id"] == values.get("field")), None)
        if field is None:
            raise ValueError("Pole wygasło. Otwórz /form.")
        if kind == "uploadcv" and field["type"] != "file":
            raise ValueError("To pole nie obsługuje załączania pliku.")
        if kind == "answer" and len(str(values.get("value", ""))) > 10000:
            raise ValueError("Odpowiedź jest zbyt długa.")
    draft.action = {"kind": kind, **values}
    return draft


def draft_messages(draft: ApplicationDraft) -> list[dict]:
    from app.telegram.views import button

    header = (
        f"<b>Formularz · #{draft.id}</b>\n{escape(draft.job.company)} · {escape(draft.job.title)}"
    )
    prefix = f"form:{draft.id}:{draft.revision}"
    if draft.status not in {"input", "review"}:
        labels = {
            "queued": "Otwieram formularz.",
            "working": "Pracuję nad formularzem.",
            "submitting": "Wysyłam zgłoszenie. Poczekaj na potwierdzenie.",
            "submitted": "Pracodawca potwierdził wysłanie zgłoszenia.",
            "uncertain": (
                "Brak pewnego potwierdzenia. Sprawdź konto pracodawcy przed kolejną próbą."
            ),
            "failed": "Przygotowanie formularza przerwane.",
            "cancelled": "Formularz anulowany.",
            "cancel_requested": "Zamykam formularz.",
        }
        message = {"text": header + "\n\n" + labels.get(draft.status, draft.status)}
        if draft.error:
            message["text"] += "\n" + escape(draft.error)
        if draft.status in {"queued", "working"}:
            message["reply_markup"] = {"inline_keyboard": [[button("Anuluj", prefix + ":cancel")]]}
        return [message]
    snapshot = draft.snapshot
    parts = [header]
    details = []
    if snapshot.get("job_reference"):
        details.append("Numer oferty pracodawcy · " + snapshot["job_reference"])
    if draft.status == "review":
        parts.append("<b>Sprawdź przed wysłaniem</b>")
        cv_label = "Załączone CV" if snapshot.get("cv_attached") else "CV wymaga załączenia"
        details.append(cv_label + " · " + draft.cv["name"])
        for key, value in draft.profile.items():
            if value and key != "answers":
                details.append(PROFILE_FIELDS.get(key, key) + " · " + value)
        for label, value in draft.answers.items():
            details.append(label + " · " + str(value))
    if snapshot.get("notice"):
        details.append(snapshot["notice"])
    if snapshot.get("text"):
        details.append(snapshot["text"])
    fields = snapshot.get("fields", [])
    for field in fields:
        value = (
            "••••" if field["type"] == "password" and field.get("value") else field.get("value", "")
        )
        details.append(f"{field['id']}. {field['label']} · {str(value) or 'brak'}")
    for detail in details:
        parts.extend(escape(detail[index : index + 600]) for index in range(0, len(detail), 600))
    messages, block = [], ""
    for part in parts:
        if block and len(block) + len(part) + 2 > 3700:
            messages.append({"text": block})
            block = ""
        block += ("\n\n" if block else "") + part
    if block:
        messages.append({"text": block})
    if snapshot.get("choosing"):
        offset = snapshot.get("options_page", 0)
        options = snapshot.get("options", [])
        rows = [
            [button(option["label"][:60], prefix + f":choice:{index}")]
            for index, option in enumerate(options[offset : offset + 20], start=offset)
        ]
        navigation = []
        if offset:
            navigation.append(button("← Poprzednie", prefix + f":choicepage:{max(0, offset - 20)}"))
        if offset + 20 < len(options):
            navigation.append(button("Następne →", prefix + f":choicepage:{offset + 20}"))
        if navigation:
            rows.append(navigation)
        if not rows:
            messages.append(
                {
                    "text": "Brak obsługiwanych odpowiedzi. Odśwież formularz "
                    "lub dokończ na stronie firmy."
                }
            )
    else:
        rows = [[button(field["label"][:60], prefix + f":field:{field['id']}")] for field in fields]
    if draft.status == "review" and snapshot.get("cv_attached") and not snapshot.get("choosing"):
        rows.append([button("Wyślij aplikację", prefix + ":send")])
    elif snapshot.get("next"):
        rows.append([button(snapshot["next"][:60], prefix + ":next")])
    if snapshot.get("back"):
        rows.append([button("Wstecz", prefix + ":back")])
    if snapshot.get("create_account"):
        rows.append([button("Otwórz zakładanie konta", prefix + ":openaccount")])
    if snapshot.get("sign_in"):
        rows.append([button("Mam konto — zaloguj", prefix + ":opensignin")])
    if snapshot.get("last_answer"):
        rows.append([button("Zapamiętaj odpowiedź dla tej firmy", prefix + ":remember")])
    rows.append([{"text": "Otwórz stronę firmy", "url": draft.job.original_url}])
    rows.append([button("Odśwież", prefix + ":refresh"), button("Anuluj", prefix + ":cancel")])
    messages.append(
        {
            "text": "Wybierz pole do uzupełnienia lub kolejne działanie.",
            "reply_markup": {"inline_keyboard": rows},
        }
    )
    return messages
