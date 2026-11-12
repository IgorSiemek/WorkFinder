from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, date, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy import update as sql_update
from sqlalchemy.orm import Session

from app.models import ApplicationActivity, BotMessage, BotState, SearchRun, utcnow
from app.services import applications as apply_service
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
/setup — dane do automatycznego uzupełniania formularzy
/applicant — profil do aplikowania
/uploadcv — prześlij CV w PDF
/form — otwarty formularz i podsumowanie
/cancel — zakończ wprowadzanie odpowiedzi
/delivery ID — ponów nieudaną wiadomość
/start — pokaż menu

Listy obsługują numer strony, np. /saved 2.
Przycisk „Aplikacja wysłana” zapisuje Twój status po wysłaniu zgłoszenia na stronie firmy."""


class BotController:
    def __init__(
        self,
        owner_id: int,
        config: dict,
        *,
        bot_id: int = 1,
        administrator_id: int | None = None,
        bot_username: str = "",
    ):
        self.owner_id = owner_id
        self.config = config
        self.bot_id = bot_id
        self.bot_username = bot_username
        self.administrator_id = administrator_id or owner_id
        self.scope = f"{bot_id}:{owner_id}"
        self.admin_scope = f"{bot_id}:{self.administrator_id}"

    def state(
        self,
        session: Session,
        key: str,
        default=None,
        *,
        scope: str | None = None,
    ):  # noqa: ANN001, ANN201
        row = session.get(BotState, f"{scope or self.scope}:{key}")
        return row.value if row else default

    def set_state(
        self,
        session: Session,
        key: str,
        value,
        *,
        scope: str | None = None,
    ) -> None:  # noqa: ANN001
        name = f"{scope or self.scope}:{key}"
        row = session.get(BotState, name)
        if row is None:
            session.add(BotState(key=name, value=value))
        else:
            row.value = value
        session.flush()

    def for_user(self, user_id: int) -> BotController:
        if user_id == self.owner_id:
            return self
        return BotController(
            user_id,
            self.config,
            bot_id=self.bot_id,
            administrator_id=self.administrator_id,
            bot_username=self.bot_username,
        )

    def allowed_user_ids(self, session: Session) -> list[int]:
        members = self.state(session, "members", [], scope=self.admin_scope)
        return [
            self.administrator_id,
            *sorted(
                {
                    value
                    for value in members
                    if type(value) is int and value > 0 and value != self.administrator_id
                }
            ),
        ]

    def create_invite(self, session: Session) -> dict:
        if self.owner_id != self.administrator_id:
            raise ValueError("Zaproszenia tworzy administrator bota.")
        if not self.bot_username:
            raise ValueError("Nazwa bota będzie dostępna po uruchomieniu połączenia z Telegramem.")
        token = secrets.token_urlsafe(24)
        self.set_state(
            session,
            "invite",
            {
                "digest": hashlib.sha256(token.encode()).hexdigest(),
                "expires_at": (utcnow() + timedelta(hours=24)).isoformat(),
            },
        )
        url = f"https://t.me/{self.bot_username}?start=join_{token}"
        return {
            "text": (
                "<b>Zaproszenie do WorkFinder</b>\n"
                "Przekaż ten link osobie, z którą chcesz dzielić profil, "
                "oferty i historię aplikacji.\n"
                "Zaproszenie jest jednorazowe i ważne przez 24 godziny.\n\n" + url
            ),
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {
                            "text": "Otwórz zaproszenie",
                            "url": url,
                        }
                    ]
                ]
            },
        }

    def claim_invite(self, session: Session, incoming: dict) -> bool:
        message = incoming.get("message", {})
        user_id = message.get("from", {}).get("id")
        chat = message.get("chat", {})
        command, _, argument = message.get("text", "").strip().partition(" ")
        if not (
            type(user_id) is int
            and user_id > 0
            and chat.get("type") == "private"
            and chat.get("id") == user_id
            and command.split("@")[0].lower() == "/start"
            and argument.startswith("join_")
        ):
            return False
        invite = self.state(session, "invite")
        if not invite or datetime.fromisoformat(invite["expires_at"]) <= utcnow():
            return False
        digest = hashlib.sha256(argument.removeprefix("join_").encode()).hexdigest()
        if not secrets.compare_digest(digest, invite["digest"]):
            return False
        members = set(self.allowed_user_ids(session)) | {user_id}
        members.discard(self.administrator_id)
        self.set_state(session, "members", sorted(members))
        self.set_state(session, "invite", None)
        self.for_user(user_id).set_state(session, "started_at", utcnow().isoformat())
        self.queue(
            session,
            f"member:{incoming['update_id']}",
            {
                "text": f"Do wspólnego WorkFinder dołączyło konto {user_id}. "
                "Dostęp sprawdzisz pod /members.",
            },
        )
        session.commit()
        return True

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
        document = update["message"].get("document")
        if document:
            if not self.state(session, "awaiting_cv"):
                raise ValueError("Aby zapisać plik jako CV, użyj najpierw /uploadcv.")
            if not document.get("file_name", "").lower().endswith(".pdf"):
                raise ValueError("Prześlij CV jako plik PDF.")
            if document.get("file_size", 0) > apply_service.MAX_CV_BYTES:
                raise ValueError("Maksymalny rozmiar CV to 10 MB.")
            if self.state(session, "cv_upload", scope=self.admin_scope):
                raise ValueError("Trwa pobieranie poprzedniego CV. Poczekaj na potwierdzenie.")
            self.set_state(
                session,
                "cv_upload",
                {
                    "file_id": document["file_id"],
                    "name": document["file_name"][:128],
                    "user_id": self.owner_id,
                    "update_id": update["update_id"],
                },
                scope=self.admin_scope,
            )
            self.set_state(session, "awaiting_cv", False)
            return [{"text": "Pobieram CV. Potwierdzę zapis po sprawdzeniu pliku."}]
        text = update["message"].get("text", "").strip()
        if text and not text.startswith("/"):
            setup_field = self.state(session, "setup_field")
            if setup_field:
                apply_service.save_profile_value(self, session, setup_field, text)
                return [apply_service.setup_next(self, session, setup_field)]
            pending = self.state(session, "form_field")
            if pending:
                draft = apply_service.request_action(
                    self,
                    session,
                    pending["draft"],
                    pending["revision"],
                    "answer",
                    field=pending["field"],
                    value="" if text == "-" else text,
                )
                self.set_state(session, "form_field", None)
                return [{"text": f"Uzupełniam pole. Formularz #{draft.id}."}]
        head, _, argument = text.partition(" ")
        command = head.split("@")[0].lower()
        command = command.removeprefix("/")
        argument = argument.strip()
        if command == "cancel":
            self.set_state(session, "setup_field", None)
            self.set_state(session, "form_field", None)
            self.set_state(session, "awaiting_cv", False)
            return [
                {
                    "text": "Wprowadzanie odpowiedzi zakończone. "
                    "Otwarty formularz znajdziesz pod /form."
                }
            ]
        if command == "setup":
            self.set_state(session, "form_field", None)
            return [apply_service.setup_next(self, session)]
        if command == "applicant":
            if argument:
                field, _, value = argument.partition(" ")
                apply_service.save_profile_value(self, session, field, value)
            return [apply_service.profile_message(self, session)]
        if command == "uploadcv":
            self.set_state(session, "awaiting_cv", True)
            return [
                {
                    "text": "Wyślij CV jako plik PDF do 10 MB. "
                    "Plik będzie wspólny dla obu kont i używany do przygotowywania aplikacji."
                }
            ]
        if command == "form":
            return apply_service.draft_messages(
                apply_service.owned_draft(self, session, int(argument) if argument else None)
            )
        if command in {"invite", "members", "revoke"}:
            if self.owner_id != self.administrator_id:
                raise ValueError("Dostępem do bota zarządza administrator.")
            if command == "invite":
                return [self.create_invite(session)]
            if command == "members":
                return [
                    {
                        "text": "<b>Konta ze wspólnym dostępem</b>\n"
                        + "\n".join(f"{user_id}" for user_id in self.allowed_user_ids(session))
                        + "\n\n/revoke ID odbiera dostęp wybranemu kontu."
                    }
                ]
            user_id = int(argument)
            if user_id == self.administrator_id:
                raise ValueError("To konto administratora bota.")
            members = set(self.allowed_user_ids(session)) - {self.administrator_id}
            members.discard(user_id)
            self.set_state(session, "members", sorted(members))
            session.execute(
                sql_update(BotMessage)
                .where(
                    BotMessage.dedup_key.startswith(f"{self.bot_id}:{user_id}:"),
                    BotMessage.status == "pending",
                )
                .values(status="cancelled")
            )
            return [{"text": f"Dostęp konta {user_id} został odebrany."}]
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
            text = HELP
            if self.owner_id == self.administrator_id:
                text += (
                    "\n\n/invite — zaproś osobę do wspólnego profilu"
                    "\n/members — konta z dostępem\n/revoke ID — odbierz dostęp"
                )
            return [{"text": text}]
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
                    if self.state(session, "search_running", scope=self.admin_scope)
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
            if self.state(session, "search_running", scope=self.admin_scope):
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
        if action == "prepare" and len(parts) == 2:
            job = get_job(session, int(parts[1]))
            # Preparation sends profile data/CV to this employer before final submission.
            return [
                {
                    "text": "Przygotowanie udostępni dane profilu i CV "
                    f"firmie {escape(job.company)} "
                    "przez jej formularz Workday.\n"
                    "Po uzupełnieniu sprawdzisz podsumowanie "
                    "i osobno zatwierdzisz wysłanie aplikacji.",
                    "reply_markup": {
                        "inline_keyboard": [
                            [button("Przygotuj formularz", f"prepareconfirm:{job.id}")]
                        ]
                    },
                }
            ]
        if action == "prepareconfirm" and len(parts) == 2:
            return apply_service.draft_messages(apply_service.prepare(self, session, int(parts[1])))
        if action == "form" and len(parts) >= 4:
            draft_id, revision, kind = int(parts[1]), int(parts[2]), parts[3]
            if kind == "remember":
                draft = apply_service.owned_draft(self, session, draft_id)
                answer = draft.snapshot.get("last_answer")
                if (
                    revision != draft.revision
                    or not answer
                    or draft.action
                    or draft.status not in {"input", "review"}
                ):
                    raise ValueError("Odpowiedź wygasła. Otwórz /form.")
                bank = dict(self.state(session, "applicant_answers", {}, scope=self.admin_scope))
                host = apply_service.workday_host(draft.job)
                saved = dict(bank.get(host, {}))
                saved[apply_service.question_key(answer["label"])] = answer["value"]
                bank[host] = saved
                self.set_state(session, "applicant_answers", bank, scope=self.admin_scope)
                draft.snapshot = {
                    key: value for key, value in draft.snapshot.items() if key != "last_answer"
                }
                draft.revision += 1
                return [
                    {
                        "text": "Odpowiedź będzie uzupełniana "
                        "przy kolejnych aplikacjach do tej firmy."
                    },
                    *apply_service.draft_messages(draft),
                ]
            if kind == "choicepage" and len(parts) == 5:
                draft = apply_service.owned_draft(self, session, draft_id)
                offset = int(parts[4])
                if (
                    revision != draft.revision
                    or draft.status not in {"input", "review"}
                    or draft.action
                    or not 0 <= offset < len(draft.snapshot.get("options", []))
                ):
                    raise ValueError("Widok pola wygasł. Otwórz /form.")
                draft.snapshot = {**draft.snapshot, "options_page": offset}
                draft.revision += 1
                return apply_service.draft_messages(draft)
            if kind == "field" and len(parts) == 5:
                draft = apply_service.owned_draft(self, session, draft_id)
                if (
                    revision != draft.revision
                    or draft.status not in {"input", "review"}
                    or draft.action
                ):
                    raise ValueError("Widok pola wygasł. Otwórz /form.")
                field_id = int(parts[4])
                field = next(
                    (item for item in draft.snapshot.get("fields", []) if item["id"] == field_id),
                    None,
                )
                if field is None:
                    raise ValueError("Pole wygasło. Otwórz /form.")
                if field["type"] == "file":
                    apply_service.request_action(
                        self, session, draft_id, revision, "uploadcv", field=field_id
                    )
                    return [{"text": "Załączam zapisane CV do wybranego pola formularza."}]
                if field["options"] or field["type"] in {
                    "combobox",
                    "select-one",
                    "radio",
                    "checkbox",
                }:
                    apply_service.request_action(
                        self, session, draft_id, revision, "options", field=field_id
                    )
                    return [{"text": "Pobieram możliwe odpowiedzi."}]
                self.set_state(session, "setup_field", None)
                self.set_state(
                    session,
                    "form_field",
                    {"draft": draft_id, "revision": revision, "field": field_id},
                )
                return [
                    {
                        "text": escape(field["label"])
                        + "\nWyślij wartość w kolejnej wiadomości. Znak - usuwa wartość. "
                        "/cancel kończy wprowadzanie."
                    }
                ]
            if kind == "choice" and len(parts) == 5:
                draft = apply_service.owned_draft(self, session, draft_id)
                options = draft.snapshot.get("options", [])
                index = int(parts[4])
                if not 0 <= index < len(options):
                    raise ValueError("Odpowiedź wygasła. Otwórz /form.")
                apply_service.request_action(
                    self,
                    session,
                    draft_id,
                    revision,
                    "answer",
                    field=draft.snapshot["choosing"],
                    value=options[index]["value"],
                )
            else:
                apply_service.request_action(self, session, draft_id, revision, kind)
            self.set_state(session, "form_field", None)
            return [{"text": "Działanie przyjęte. Poczekaj na aktualny formularz."}]
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
