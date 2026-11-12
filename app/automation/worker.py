from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import suppress
from datetime import timedelta

from sqlalchemy import select

from app.automation.workday import JobUnavailable, WorkdayForm
from app.models import ApplicationDraft, utcnow
from app.services.applications import ACTIVE, MAX_CV_BYTES, draft_messages
from app.services.bot_config import config_path
from app.services.progress import change_status

logger = logging.getLogger(__name__)


class ApplicationWorker:
    def __init__(self, client, controller, sessions):  # noqa: ANN001
        self.client = client
        self.controller = controller
        self.sessions = sessions
        self.form = None
        self.draft_id = None
        self.directory = config_path().parent / "applications"

    def notify(self, session, draft: ApplicationDraft) -> None:  # noqa: ANN001
        member = self.controller.for_user(draft.user_id)
        for index, message in enumerate(draft_messages(draft)):
            member.queue(
                session, f"form:{draft.id}:{draft.revision}:{draft.status}:{index}", message
            )

    def recover(self) -> None:
        with self.sessions() as session:
            for draft in session.scalars(
                select(ApplicationDraft).where(ApplicationDraft.status.in_(ACTIVE))
            ):
                if draft.status == "submitting":
                    draft.status = "uncertain"
                    draft.error = "Proces przerwany podczas wysyłania. Sprawdź konto pracodawcy."
                else:
                    draft.status = "failed"
                    draft.error = (
                        "Sesja przeglądarki została przerwana. Przygotuj formularz ponownie."
                    )
                draft.action = {}
                draft.revision += 1
                self.notify(session, draft)
            session.commit()

    async def upload_cv(self) -> None:
        with self.sessions() as session:
            upload = self.controller.state(session, "cv_upload", scope=self.controller.admin_scope)
            if not upload:
                return
            allowed = upload["user_id"] in self.controller.allowed_user_ids(session)
        if not allowed:
            with self.sessions() as session:
                self.controller.set_state(
                    session, "cv_upload", None, scope=self.controller.admin_scope
                )
                session.commit()
            return
        text = ""
        document = None
        try:
            data = await self.client.download_file(upload["file_id"], MAX_CV_BYTES)
            if not data.startswith(b"%PDF-"):
                raise ValueError("Prześlij poprawny plik PDF.")
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self.directory / (hashlib.sha256(data).hexdigest() + ".pdf")
            if (
                not path.exists()
                or hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(data).digest()
            ):
                temporary = path.with_suffix(".tmp")
                temporary.write_bytes(data)
                temporary.chmod(0o600)
                temporary.replace(path)
            document = {
                "path": str(path),
                "name": upload["name"],
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            text = (
                "CV zapisane. Użyj /setup, aby uzupełnić dane, "
                "albo wybierz Przygotuj aplikację przy ofercie."
            )
        except Exception:
            text = "Nie udało się zapisać CV. Prześlij poprawny PDF do 10 MB przez /uploadcv."
        with self.sessions() as session:
            if upload["user_id"] not in self.controller.allowed_user_ids(session):
                self.controller.set_state(
                    session, "cv_upload", None, scope=self.controller.admin_scope
                )
                session.commit()
                return
            if document:
                self.controller.set_state(
                    session, "application_cv", document, scope=self.controller.admin_scope
                )
            self.controller.set_state(session, "cv_upload", None, scope=self.controller.admin_scope)
            self.controller.for_user(upload["user_id"]).queue(
                session, f"cv:{upload['update_id']}", {"text": text}
            )
            session.commit()

    async def close_form(self) -> None:
        if self.form:
            await self.form.close()
        self.form, self.draft_id = None, None

    async def step(self) -> None:
        await self.upload_cv()
        with self.sessions() as session:
            draft = session.scalar(
                select(ApplicationDraft)
                .where(ApplicationDraft.status.in_(ACTIVE))
                .order_by(ApplicationDraft.id)
                .limit(1)
            )
            if draft is None:
                await self.close_form()
                return
            if draft.user_id not in self.controller.allowed_user_ids(session):
                draft.status = "cancel_requested"
            if draft.updated_at < utcnow() - timedelta(hours=1):
                draft.status = "cancel_requested"
            if draft.status == "cancel_requested":
                draft.status, draft.action = "cancelled", {}
                draft.revision += 1
                self.notify(session, draft)
                session.commit()
                await self.close_form()
                return
            if draft.status != "queued" and not draft.action:
                return
            preparing = draft.status == "queued"
            action = dict(draft.action)
            draft.action, draft.status = {}, "working"
            session.commit()
            identifier = draft.id
            job, profile, cv, snapshot = draft.job, draft.profile, draft.cv, draft.snapshot
            # Freeze data before releasing the database connection for browser/network waits.
        try:
            if preparing:
                await self.close_form()
                self.form, self.draft_id = WorkdayForm(), identifier
                result = await self.form.start(job, profile, cv, self.directory / "sessions")
            elif not self.form or self.draft_id != identifier:
                raise ValueError("Sesja wygasła. Przygotuj formularz ponownie.")
            elif action["kind"] == "send":
                await self.send(identifier, snapshot, profile, cv)
                return
            else:
                result = await self.perform(action, snapshot, profile, cv)
            with self.sessions() as session:
                draft = session.get(ApplicationDraft, identifier)
                # A cancellation or revocation arriving during a network call wins.
                if (
                    draft.status == "cancel_requested"
                    or draft.user_id not in self.controller.allowed_user_ids(session)
                ):
                    draft.status, draft.action = "cancelled", {}
                else:
                    draft.status = "review" if result.get("submit") else "input"
                    draft.snapshot = result
                    if action.get("kind") == "answer":
                        field = next(
                            item for item in snapshot["fields"] if item["id"] == action["field"]
                        )
                        answer = "••••" if field["type"] == "password" else action["value"]
                        draft.answers = {**draft.answers, field["label"]: answer}
                        if field["type"] != "password":
                            draft.snapshot = {
                                **draft.snapshot,
                                "last_answer": {"label": field["label"], "value": answer},
                            }
                draft.error = None
                draft.revision += 1
                self.notify(session, draft)
                session.commit()
        except Exception as exc:
            # Error strings from browser libraries may include filled values or request URLs.
            logger.warning("Application form step failed (%s)", type(exc).__name__)
            with self.sessions() as session:
                current = session.get(ApplicationDraft, identifier)
                can_recover = (
                    not preparing
                    and current.status == "working"
                    and current.user_id in self.controller.allowed_user_ids(session)
                )
            if can_recover and self.form:
                try:
                    result = await self.form.inspect({}, {}, autofill=False)
                except Exception:
                    pass
                else:
                    result["notice"] = (
                        "Pole lub przycisk wymaga sprawdzenia. "
                        "Wybierz odpowiedź ponownie lub użyj strony pracodawcy."
                    )
                    with self.sessions() as session:
                        current = session.get(ApplicationDraft, identifier)
                        if current.status == "cancel_requested":
                            current.status = "cancelled"
                        else:
                            current.snapshot = result
                            current.status = "review" if result["submit"] else "input"
                        current.revision += 1
                        self.notify(session, current)
                        session.commit()
                    return
            with self.sessions() as session:
                draft = session.get(ApplicationDraft, identifier)
                if draft.status == "submitting":
                    draft.status = "uncertain"
                    draft.error = (
                        "Brak potwierdzenia wysłania. Sprawdź konto pracodawcy przed kolejną próbą."
                    )
                else:
                    draft.status = "failed"
                    if isinstance(exc, JobUnavailable):
                        draft.error = str(exc)
                    else:
                        draft.error = (
                            "Strona wymaga ręcznej obsługi albo sesja wygasła. "
                            "Otwórz ofertę na stronie firmy."
                        )
                draft.action = {}
                draft.revision += 1
                self.notify(session, draft)
                session.commit()
            await self.close_form()

    async def perform(self, action: dict, snapshot: dict, profile: dict, cv: dict) -> dict:
        kind = action["kind"]
        if kind in {"answer", "options", "uploadcv"}:
            field = next(item for item in snapshot["fields"] if item["id"] == action["field"])
            if kind == "options":
                options = await self.form.options(field)
                return {**snapshot, "choosing": field["id"], "options": options}
            if kind == "uploadcv":
                await self.form.upload_cv(field, cv)
                return await self.form.inspect(profile, cv)
            await self.form.fill(field, action["value"])
        elif kind == "next":
            await self.form.advance()
        elif kind == "back":
            await self.form.back()
        elif kind == "openaccount":
            await self.form.open_account()
        elif kind == "opensignin":
            await self.form.open_sign_in()
        return await self.form.inspect(profile, cv)

    async def send(self, identifier: int, snapshot: dict, profile: dict, cv: dict) -> None:
        # Recheck the exact review page before committing the irreversible sending state.
        try:
            await self.form.validate_review(snapshot, profile, cv)
        except ValueError:
            result = await self.form.inspect(profile, cv, autofill=False)
            result["notice"] = "Formularz zmienił się. Sprawdź go ponownie przed wysłaniem."
            with self.sessions() as session:
                draft = session.get(ApplicationDraft, identifier)
                draft.snapshot = result
                draft.status = "review" if result["submit"] else "input"
                draft.revision += 1
                self.notify(session, draft)
                session.commit()
            return
        with self.sessions() as session:
            draft = session.get(ApplicationDraft, identifier)
            if (
                draft.status == "cancel_requested"
                or draft.user_id not in self.controller.allowed_user_ids(session)
            ):
                draft.status = "cancelled"
                session.commit()
                return
            if (
                not draft.job.is_active
                or draft.job.ignored
                or draft.job.application.status != "Preparing"
            ):
                raise ValueError("Oferta lub status aplikacji zmieniły się.")
            draft.status = "submitting"
            session.commit()
        receipt = await self.form.submit()
        with self.sessions() as session:
            draft = session.get(ApplicationDraft, identifier)
            draft.status, draft.receipt = "submitted", receipt
            draft.revision += 1
            application = change_status(
                session, draft.job, "Applied", today=self.controller.today()
            )
            application.cv_version = cv["name"]
            self.notify(session, draft)
            session.commit()
        await self.close_form()

    async def run(self) -> None:
        self.recover()
        try:
            while True:
                await self.step()
                await asyncio.sleep(1)
        finally:
            with suppress(Exception):
                await self.close_form()
