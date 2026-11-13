from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from app.database import SessionLocal
from app.profiles.context import activate_profile
from app.profiles.controller import PersonalController
from app.profiles.store import ProfileStore

logger = logging.getLogger(__name__)


class ProfileRouter:
    """One Telegram poller; each private profile runs with its own database context."""

    def __init__(self, legacy, store: ProfileStore):
        self.legacy = legacy
        self.store = store
        self.children = {}
        self.lock = asyncio.Lock()

    def legacy_state(self):
        with SessionLocal() as session:
            return (
                self.legacy.controller.state(session, "offset", 0),
                set(self.legacy.controller.allowed_user_ids(session)),
            )

    def acknowledge(self, update_id: int):
        with SessionLocal() as session:
            current = self.legacy.controller.state(session, "offset", 0)
            self.legacy.controller.set_state(session, "offset", max(current, update_id + 1))
            session.commit()

    async def ensure(self, record):
        from app.telegram.runtime import BotRuntime, initialize

        key = record["profile_key"]
        async with self.lock:
            child = self.children.get(key)
            if child and not child[2].done():
                return child
            if child:
                child[0].engine.dispose()
            context, _ = await asyncio.to_thread(self.store.open, key)
            try:
                with activate_profile(context):
                    config = await asyncio.to_thread(initialize)
                    controller = PersonalController(
                        record["user_id"],
                        config,
                        bot_id=self.legacy.controller.bot_id,
                        bot_username=self.legacy.controller.bot_username,
                    )
                    runtime = BotRuntime(self.legacy.client, controller)
                    task = asyncio.create_task(self.serve(runtime), name=f"profile:{key}")
                child = (context, runtime, task)
                self.children[key] = child
                await asyncio.sleep(0)  # Recover durable state before handling the first command.
                return child
            except BaseException:
                context.engine.dispose()
                raise

    async def serve(self, runtime):
        try:
            await runtime.run(poll=False)
        except Exception:
            # A broken friend's profile must not stop the original user's workers.
            logger.exception("Private profile worker stopped")

    async def dispatch(self, update: dict) -> bool:
        offset, reserved = await asyncio.to_thread(self.legacy_state)
        if update["update_id"] < offset:
            return False
        callback = update.get("callback_query")
        incoming = callback or update.get("message", {})
        user_id = incoming.get("from", {}).get("id")
        message = incoming.get("message", {}) if callback else incoming
        chat = message.get("chat", {})
        if type(user_id) is not int or chat.get("type") != "private" or chat.get("id") != user_id:
            return await asyncio.to_thread(self.legacy.handle_update, update)
        if user_id in reserved:
            return await asyncio.to_thread(self.legacy.handle_update, update)
        try:
            record = await asyncio.to_thread(self.store.for_user, user_id)
        except Exception:
            logger.exception("Private registry lookup failed")
            await asyncio.to_thread(self.acknowledge, update["update_id"])
            return False
        text = incoming.get("text", "") if not callback else ""
        command, _, argument = text.strip().partition(" ")
        joining = command.split("@")[0].lower() == "/start" and argument.startswith("profile_")
        if record is None and joining and user_id not in reserved:
            try:
                record = await asyncio.to_thread(
                    self.store.claim, argument.removeprefix("profile_"), user_id, reserved=reserved
                )
            except Exception:
                logger.exception("Private profile invitation unavailable")
                await asyncio.to_thread(self.acknowledge, update["update_id"])
                return False
            if record is None:
                await self.legacy.client.call(
                    "sendMessage",
                    chat_id=user_id,
                    text="Kod profilu jest nieprawidłowy, wykorzystany albo wygasł.",
                )
                await asyncio.to_thread(self.acknowledge, update["update_id"])
                return False
        if record is None:
            return await asyncio.to_thread(self.legacy.handle_update, update)
        if not record["enabled"]:
            await asyncio.to_thread(self.acknowledge, update["update_id"])
            return False
        try:
            context, runtime, _ = await self.ensure(record)
            with activate_profile(context):
                authorized = await asyncio.to_thread(runtime.handle_update, update)
        except Exception:
            logger.exception("Private profile update failed")
            await self.legacy.client.call(
                "sendMessage",
                chat_id=user_id,
                text="Twój profil jest chwilowo niedostępny. Spróbuj ponownie za chwilę.",
            )
            authorized = False
        await asyncio.to_thread(self.acknowledge, update["update_id"])
        return authorized

    async def run(self):
        try:
            while True:
                try:
                    records = await asyncio.to_thread(self.store.profiles)
                except Exception:
                    logger.exception(
                        "Private profile registry unavailable; original profile continues"
                    )
                    records = []
                for record in records:
                    if record["enabled"] and record["user_id"] is not None:
                        child = self.children.get(record["profile_key"])
                        if child is None or child[2].done():
                            try:
                                await self.ensure(record)
                            except Exception:
                                logger.exception("Cannot start private profile")
                await asyncio.sleep(10)
        finally:
            for _, _, task in self.children.values():
                task.cancel()
            for context, _, task in self.children.values():
                with suppress(asyncio.CancelledError):
                    await task
                context.engine.dispose()
