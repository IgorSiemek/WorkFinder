from __future__ import annotations

from datetime import UTC, date
from html import escape
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ApplicationActivity, BotMessage, BotState, SearchRun, utcnow
from app.services.bot_config import apply_config, load_config, next_search
from app.services.jobs import (
    add_manual_job,
    edit_application,
    follow_ups,
    get_job,
    job_action,
    list_jobs,
)
from app.services.progress import APPLICATION_STATUSES
from app.telegram.views import MENU, button, job_card, status_keyboard

HELP = """<b>WorkFinder</b>
/new — nowe oferty z ostatnich 7 dni
/all — aktywne staże
/saved — zapisane oferty
/applications — historia aplikacji
/ignored — pominięte oferty
/find tekst — wyszukaj w tytułach i firmach
/followups — działania na dziś i zaległe
/job ID — szczegóły oferty
/note ID treść — ustaw notatkę (znak - usuwa)
/date ID RRRR-MM-DD — ustaw przypomnienie (znak - usuwa)
/cv ID nazwa — ustaw wersję CV (znak - usuwa)
/history ID — historia zmian oferty
/add Tytuł stażu | Firma | https://link | Hybrid | Opis
/search — uruchom wyszukiwanie
/health — stan wyszukiwania i wiadomości
/pause /resume — wstrzymaj / wznów powiadomienia
/config — ustawienia i nazwy źródeł
/reload — wczytaj konfigurację JSON
/backup — utwórz backup bazy
/excel — ponów synchronizację Excela
/dates ID — sprawdź datę publikacji
/delivery ID — ponów nieudaną wiadomość
/start — pokaż menu

Listy obsługują numer strony, np. /saved 2.
Przycisk „Aplikacja wysłana” zapisuje Twój status po wysłaniu zgłoszenia na stronie firmy."""


class BotController:
    def __init__(self, owner_id: int, config: dict, *, bot_id: int = 1):
        self.owner_id = owner_id
        self.config = config
        self.scope = f"{bot_id}:{owner_id}"

    def state(self, session: Session, key: str, default=None):  # noqa: ANN001, ANN201
        row = session.get(BotState, f"{self.scope}:{key}")
        return row.value if row else default

    def set_state(self, session: Session, key: str, value) -> None:  # noqa: ANN001
        name = f"{self.scope}:{key}"
        row = session.get(BotState, name)
        if row is None:
            session.add(BotState(key=name, value=value))
        else:
            row.value = value
        session.flush()

    def queue(
        self,
        session: Session,
        key: str,
        payload: dict,
        method: str = "sendMessage",
    ) -> None:
        key = f"{self.scope}:{key}"
        if session.scalar(select(BotMessage.id).where(BotMessage.dedup_key == key)):
            return
        session.add(
            BotMessage(
                dedup_key=key,
                method=method,
                payload={
                    "chat_id": self.owner_id,
                    "parse_mode": "HTML",
                    "link_preview_options": {"is_disabled": True},
                    **payload,
                },
            )
        )
        session.flush()

    def authorized(self, update: dict) -> bool:
        callback = update.get("callback_query")
        message = callback.get("message", {}) if callback else update.get("message", {})
        sender = callback.get("from", {}) if callback else message.get("from", {})
        chat = message.get("chat", {})
        return sender.get("id") == self.owner_id and (
            chat.get("type") == "private" and chat.get("id") == self.owner_id
        )

    def handle(self, session: Session, update: dict) -> None:
        update_id = update["update_id"]
        if update_id < self.state(session, "offset", 0):
            return
        # Start the outer write transaction before the savepoint (also on SQLite).
        self.set_state(session, "offset", update_id + 1)
        if self.authorized(update):
            callback = update.get("callback_query")
            try:
                with session.begin_nested():
                    replies = self.dispatch(session, update)
                    for index, reply in enumerate(replies):
                        method = "sendMessage"
                        if callback and index == 0 and reply.pop("edit", False):
                            method = "editMessageText"
                            reply["message_id"] = callback["message"]["message_id"]
                        self.queue(session, f"update:{update_id}:{index}", reply, method)
            except (ValueError, OSError) as exc:
                self.queue(session, f"update:{update_id}:error", {"text": escape(str(exc)[:1000])})
        session.commit()

    def dispatch(self, session: Session, update: dict) -> list[dict]:
        callback = update.get("callback_query")
        if callback:
            return self.callback(session, callback.get("data", ""))
        text = update["message"].get("text", "").strip()
        head, _, argument = text.partition(" ")
        command = head.split("@")[0].lower()
        command = command.removeprefix("/")
        argument = argument.strip()
        if command == "start":
            if not self.state(session, "started_at"):
                self.set_state(session, "started_at", utcnow().isoformat())
            return [
                {
                    "text": "<b>WorkFinder</b>\nWybierz oferty lub uruchom wyszukiwanie.",
                    "reply_markup": MENU,
                }
            ]
        if command == "help":
            return [{"text": HELP}]
        views = {
            "new": "new",
            "all": "all",
            "saved": "saved",
            "applications": "applications",
            "ignored": "ignored",
        }
        if command in views:
            return self.listing(session, views[command], int(argument or "1"))
        if command == "find":
            if not argument:
                raise ValueError("Podaj szukany tekst po /find")
            self.set_state(session, "find_query", argument[:300])
            return self.listing(session, "find", 1)
        if command == "job":
            return [job_card(get_job(session, int(argument)), detailed=True)]
        if command == "search":
            self.set_state(session, "search_requested", True)
            return [
                {
                    "text": "Wyszukiwanie trwa."
                    if self.state(session, "search_running")
                    else "Wyszukiwanie zaplanowane. Wynik dostaniesz po zakończeniu."
                }
            ]
        if command in {"pause", "resume"}:
            self.set_state(session, "paused", command == "pause")
            return [
                {
                    "text": "Powiadomienia wstrzymane."
                    if command == "pause"
                    else "Powiadomienia włączone."
                }
            ]
        if command in {"note", "date", "cv"}:
            identifier, _, value = argument.partition(" ")
            if not value:
                raise ValueError(f"Użyj /{command} ID wartość. Znak - usuwa wartość.")
            fields = {"note": "notes", "date": "next_action", "cv": "cv_version"}
            job = edit_application(
                session,
                int(identifier),
                **{
                    fields[command]: "" if value == "-" else value,
                },
                today=self.today(),
            )
            return [job_card(job, detailed=True)]
        if command == "history":
            job = get_job(session, int(argument))
            events = session.scalars(
                select(ApplicationActivity)
                .where(
                    ApplicationActivity.application_id
                    == (job.application.id if job.application else -1)
                )
                .order_by(ApplicationActivity.id.desc())
                .limit(10)
            ).all()
            lines = [f"<b>Ostatnie zmiany · #{job.id}</b>"] + [
                f"{event.at:%Y-%m-%d %H:%M} UTC · {escape(event.description[:220])}"
                for event in events
            ]
            return [{"text": "\n".join(lines) if events else "Brak historii zmian."}]
        if command == "add":
            return [job_card(add_manual_job(session, argument), detailed=True)]
        if command == "followups":
            today = self.today()
            applications = follow_ups(session, today)
            page = max(1, int(argument or "1"))
            size = self.config["page_size"]
            return [
                {"text": f"Działania na dziś i zaległe · {len(applications)}. Strona {page}."},
                *[job_card(item.job) for item in applications[(page - 1) * size : page * size]],
            ]
        if command in {"backup", "excel", "dates"}:
            value = int(argument) if command == "dates" else True
            if command == "dates":
                get_job(session, value)
            self.set_state(session, f"{command}_requested", value)
            return [{"text": "Zadanie zaplanowane. Wynik dostaniesz po zakończeniu."}]
        if command == "reload":
            if self.state(session, "search_running"):
                raise ValueError("Wczytaj konfigurację ponownie po zakończeniu wyszukiwania.")
            # Runtime loads the file and swaps the config after committing database changes.
            self.set_state(session, "reload_requested", True)
            return [{"text": "Zaplanowano wczytanie konfiguracji."}]
        if command == "config":
            from app.scrapers.registry import enabled_sources

            config = self.config
            sources = ", ".join(config["sources"]) or "wszystkie dostępne"
            available = "\n".join(source.name for source in enabled_sources())
            return [
                {
                    "text": escape(
                        f"Strefa · {config['timezone']}\nWyszukiwanie · "
                        f"{', '.join(config['search_times']) or 'ręczne'}\n"
                        f"Przypomnienia · {config['reminder_time']}\n"
                        f"Próg powiadomień · {config['minimum_score']}/100\nŹródła · {sources}\n\n"
                        f"Dostępne nazwy źródeł\n{available}\n\n"
                        "Edytuj plik JSON i użyj /reload."
                    )
                }
            ]
        if command == "health":
            return [self.health(session)]
        if command == "delivery":
            message = session.get(BotMessage, int(argument))
            if not message or not message.dedup_key.startswith(f"{self.scope}:"):
                raise ValueError("Nie znaleziono wiadomości")
            if message.status not in {"uncertain", "failed"}:
                raise ValueError("Wiadomość jest już wysłana lub czeka w kolejce")
            message.status = "pending"
            message.available_at = utcnow()
            message.error = None
            return [{"text": "Wiadomość trafiła do ponownego wysłania."}]
        return [
            {
                "text": "Wybierz akcję z menu. Listę poleceń znajdziesz pod /help.",
                "reply_markup": MENU,
            }
        ]

    def callback(self, session: Session, data: str) -> list[dict]:
        parts = data.split(":")
        action = parts[0]
        if action == "search":
            self.set_state(session, "search_requested", True)
            return [{"text": "Wyszukiwanie zaplanowane."}]
        if action == "list" and len(parts) == 3:
            return self.listing(session, parts[1], int(parts[2]))
        if len(parts) < 2:
            raise ValueError("Nieprawidłowy przycisk")
        job_id = int(parts[1])
        if action == "statuses":
            job = get_job(session, job_id)
            return [
                {
                    "text": f"Wybierz status · {escape(job.title)}",
                    "edit": True,
                    "reply_markup": status_keyboard(job.id),
                }
            ]
        if action == "status" and len(parts) == 3:
            index = int(parts[2])
            if not 0 <= index < len(APPLICATION_STATUSES):
                raise ValueError("Nieprawidłowy status")
            job = edit_application(
                session,
                job_id,
                status=APPLICATION_STATUSES[index],
                today=self.today(),
            )
        elif action == "job":
            job = get_job(session, job_id)
        else:
            job = job_action(session, job_id, action, today=self.today())
        return [{**job_card(job, detailed=action == "job"), "edit": True}]

    def listing(self, session: Session, view: str, page: int) -> list[dict]:
        labels = {
            "new": "Nowe oferty",
            "all": "Aktywne staże",
            "saved": "Zapisane",
            "applications": "Moje aplikacje",
            "ignored": "Pominięte",
            "find": "Wyniki wyszukiwania",
        }
        if view not in labels or not 1 <= page <= 100000:
            raise ValueError("Nieprawidłowa lista lub numer strony")
        size = self.config["page_size"]
        query = self.state(session, "find_query", "") if view == "find" else ""
        jobs, total = list_jobs(
            session,
            "all" if view == "find" else view,
            page=page,
            page_size=size,
            minimum_score=self.config["minimum_score"],
            query=query,
        )
        pages = max(1, (total + size - 1) // size)
        result = [{"text": f"<b>{labels[view]}</b> · {total}\nStrona {page}/{pages}"}]
        if query:
            result[0]["text"] += "\n" + escape(query)
        result.extend(job_card(job) for job in jobs)
        navigation = []
        if page > 1:
            navigation.append(button("← Poprzednie", f"list:{view}:{page - 1}"))
        if page < pages:
            navigation.append(button("Następne →", f"list:{view}:{page + 1}"))
        if navigation:
            result.append(
                {
                    "text": "Przeglądaj dalej",
                    "reply_markup": {
                        "inline_keyboard": [navigation],
                    },
                }
            )
        return result

    def health(self, session: Session) -> dict:
        run = session.scalar(select(SearchRun).order_by(SearchRun.id.desc()).limit(1))
        lines = ["<b>Stan WorkFinder</b>"]
        if run:
            lines.append(f"Ostatnie wyszukiwanie · {run.started_at:%Y-%m-%d %H:%M} UTC")
            lines.append(f"{escape(run.status)} · nowych {run.new_jobs} · błędów {run.errors}")
            for provider in run.provider_runs:
                if provider.status != "success":
                    lines.append(escape(f"{provider.provider} · {provider.status}"))
        else:
            lines.append("Pierwsze wyszukiwanie przed Tobą.")
        counts = session.execute(
            select(BotMessage.status, func.count())
            .where(BotMessage.dedup_key.startswith(f"{self.scope}:"))
            .group_by(BotMessage.status)
        ).all()
        lines.extend(f"Wiadomości {escape(status)} · {count}" for status, count in counts)
        failures = session.scalars(
            select(BotMessage)
            .where(
                BotMessage.dedup_key.startswith(f"{self.scope}:"),
                BotMessage.status.in_(["uncertain", "failed"]),
            )
            .order_by(BotMessage.id.desc())
            .limit(5)
        ).all()
        for message in failures:
            lines.append(f"#{message.id} · {message.status} · /delivery {message.id}")
        if failures:
            lines.append(
                "uncertain oznacza niepotwierdzone doręczenie. Ponowienie może wysłać kopię."
            )
        lines.append(
            "Powiadomienia · " + ("wstrzymane" if self.state(session, "paused") else "włączone")
        )
        return {"text": "\n".join(lines)}

    def today(self) -> date:
        return utcnow().replace(tzinfo=UTC).astimezone(ZoneInfo(self.config["timezone"])).date()

    def reload(self, session: Session) -> None:
        config = load_config()
        apply_config(session, config)
        due = next_search(config, utcnow())
        self.set_state(session, "next_search", due.isoformat() if due else None)
        self.set_state(session, "reload_requested", False)
        self.queue(session, f"reload:{utcnow().isoformat()}", {"text": "Konfiguracja wczytana."})
        session.commit()
        self.config = config
