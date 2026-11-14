from __future__ import annotations

import sqlite3

from app.models import utcnow
from app.profiles.context import current_profile
from app.profiles.store import ProfileStore, validate_personal_config
from app.services.bot_config import apply_config, load_config, next_search
from app.telegram.controller import BotController


class PersonalController(BotController):
    def allowed_user_ids(self, session):
        profile = current_profile()
        if profile is None:
            return []
        try:
            record = ProfileStore(profile.directory.parent).for_user(self.owner_id)
        except (OSError, sqlite3.Error):
            return []
        if record and record["enabled"] and record["profile_key"] == profile.key:
            return [self.owner_id]
        return []

    def for_user(self, user_id):
        if user_id != self.owner_id:
            raise ValueError("Konto nie należy do tego profilu")
        return self

    def create_invite(self, session):
        raise ValueError("Osobny profil tworzy administrator WorkFinder; ten profil jest prywatny.")

    def claim_invite(self, session, incoming):
        return False

    def dispatch(self, session, update):
        command = update.get("message", {}).get("text", "").split(" ", 1)[0].split("@")[0]
        if command == "/profile":
            from html import escape

            return [{"text": "Twój profil · " + escape(self.config["profile"]["name"])}]
        result = super().dispatch(session, update)
        for reply in result:
            if "text" in reply:
                reply["text"] = (
                    reply["text"]
                    .replace("Aktywne staże", "Aktywne oferty")
                    .replace("aktywne staże", "aktywne oferty")
                    .replace("Tytuł stażu", "Tytuł oferty")
                    .replace(
                        "Plik będzie wspólny dla obu kont",
                        "Plik będzie dostępny wyłącznie w Twoim profilu",
                    )
                )
        return result

    def reload(self, session):
        config = validate_personal_config(load_config())
        context = current_profile()
        previous = context.scope
        context.scope = config["job_scope"]
        try:
            apply_config(session, config)
            due = next_search(config, utcnow())
            self.set_state(session, "next_search", due.isoformat() if due else None)
            self.set_state(session, "reload_requested", False)
            self.queue(
                session, f"reload:{utcnow().isoformat()}", {"text": "Konfiguracja wczytana."}
            )
            session.commit()
            self.config = config
        except Exception:
            context.scope = previous
            raise
