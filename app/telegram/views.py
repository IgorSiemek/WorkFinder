from __future__ import annotations

from html import escape

from app.models import Job
from app.services.progress import APPLICATION_STATUSES

STATUS_LABELS = (
    "Zapisana",
    "Przygotowuję",
    "Aplikacja wysłana",
    "Rozmowa z rekruterem",
    "Rozmowa techniczna / case",
    "Rozmowa z managerem",
    "Rozmowa finałowa",
    "Oferta pracy",
    "Odrzucona",
    "Wycofana",
    "Wygasła",
)
STATUSES = dict(zip(APPLICATION_STATUSES, STATUS_LABELS, strict=True))
MENU = {
    "inline_keyboard": [
        [
            {"text": "Nowe oferty", "callback_data": "list:new:1"},
            {"text": "Zapisane", "callback_data": "list:saved:1"},
        ],
        [
            {"text": "Moje aplikacje", "callback_data": "list:applications:1"},
            {"text": "Szukaj teraz", "callback_data": "search"},
        ],
    ]
}


def button(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def job_card(job: Job, *, detailed: bool = False) -> dict:
    text = (
        f"<b>{escape(job.title[:300])}</b>\n{escape(job.company[:200])}\n"
        f"{escape(job.location[:240])} · {escape(job.remote_type)}\n"
        f"Dopasowanie <b>{job.match_score}/100</b> · #{job.id}\n"
        f"Publikacja {job.date_posted.isoformat() if job.date_posted else 'brak daty'}\n"
        f"Znaleziono {job.date_discovered:%Y-%m-%d}"
    )
    if job.skills:
        text += "\nUmiejętności · " + escape(", ".join(job.skills[:6])[:250])
    if job.missing_skills:
        text += "\nBraki w profilu · " + escape(", ".join(job.missing_skills[:4])[:200])
    if job.application:
        app = job.application
        text += "\nStatus · " + STATUSES.get(app.status, escape(app.status))
        if app.next_action_date:
            text += f"\nKolejne działanie · {app.next_action_date}"
        if detailed:
            text += "\nCV · " + escape(app.cv_version or "brak")
            if app.notes:
                text += "\nNotatka · " + escape(app.notes[:400])
    if not job.is_active:
        text += "\nOferta nieaktywna"
    if job.ignored:
        text += "\nOferta pominięta"
    if detailed:
        evidence = (job.raw_payload or {}).get("_publication_check", {})
        if evidence:
            text += "\nWeryfikacja daty · " + escape(str(evidence.get("checked_on", "brak")))
            text += "\nŹródło daty · " + escape(str(evidence.get("method", "brak"))[:200])
            if evidence.get("status") == "relative":
                text += "\nSzacunek źródła · " + escape(str(evidence.get("evidence", ""))[:200])
        reasons = "\n".join(
            f"{item['points']:+} {item['label']}" for item in job.match_explanation[:8]
        )
        text += "\n\n" + escape(reasons[:500])
        text += "\n\n" + escape(job.description[:1100] or "Brak opisu")
    rows = [[{"text": "Otwórz ofertę", "url": job.original_url}]]
    actions = []
    if not job.application:
        actions.append(button("Zapisz", f"save:{job.id}"))
    if (
        job.is_active
        and not job.ignored
        and (not job.application or job.application.status in {"Saved", "Preparing"})
    ):
        actions.append(button("Aplikacja wysłana", f"apply:{job.id}"))
    if actions:
        rows.append(actions)
    rows.append(
        [
            button(
                "Przywróć" if job.ignored else "Pomiń",
                f"{'restore' if job.ignored else 'ignore'}:{job.id}",
            ),
            button("Status", f"statuses:{job.id}"),
            button("Szczegóły", f"job:{job.id}"),
        ]
    )
    return {"text": text, "reply_markup": {"inline_keyboard": rows}}


def status_keyboard(job_id: int) -> dict:
    return {
        "inline_keyboard": [
            [button(label, f"status:{job_id}:{index}")] for index, label in enumerate(STATUS_LABELS)
        ]
    }
