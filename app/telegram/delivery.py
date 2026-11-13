from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import String, case, select, update
from sqlalchemy.orm import Session

from app.models import BotMessage, Job, utcnow
from app.profiles.scope import active_scope, job_filter
from app.services.jobs import follow_ups, get_job
from app.services.progress import CLOSED_STATUSES
from app.telegram.client import TelegramError
from app.telegram.controller import BotController
from app.telegram.views import job_card


def queue_notifications(session: Session, controller: BotController, now: datetime) -> None:
    started = controller.state(session, "started_at")
    if not started or controller.state(session, "paused"):
        return
    prefix = f"{controller.scope}:job:"
    # A correlated EXISTS keeps the scan bounded even after years of searches.
    existing = (
        select(BotMessage.id).where(BotMessage.dedup_key == prefix + Job.id.cast(String)).exists()
    )
    jobs = session.scalars(
        select(Job)
        .where(
            Job.is_demo.is_(False),
            job_filter(),
            Job.is_active.is_(True),
            Job.ignored.is_(False),
            ~Job.application.has(),
            Job.match_score >= controller.config["minimum_score"],
            Job.date_discovered >= datetime.fromisoformat(started),
            ~existing,
        )
        .order_by(Job.match_score.desc(), Job.id)
        .limit(100)
    ).all()
    for job in jobs:
        controller.queue(session, f"job:{job.id}", job_card(job))
    local = now.replace(tzinfo=UTC).astimezone(ZoneInfo(controller.config["timezone"]))
    if local.strftime("%H:%M") >= controller.config["reminder_time"]:
        for application in follow_ups(session, local.date()):
            controller.queue(
                session,
                f"reminder:{application.job_id}:{application.next_action_date}:{local.date()}",
                {
                    **job_card(application.job),
                    "text": "<b>Przypomnienie</b>\n" + job_card(application.job)["text"],
                },
            )
    session.commit()


def recover_delivery(session: Session, controller: BotController) -> None:
    session.execute(
        update(BotMessage)
        .where(
            BotMessage.dedup_key.startswith(f"{controller.scope}:"),
            BotMessage.status == "sending",
        )
        .values(status="uncertain", error="Proces przerwany podczas wysyłania")
    )
    session.commit()


def prepare_message(session: Session, controller: BotController) -> BotMessage | None:
    if controller.owner_id not in controller.allowed_user_ids(session):
        return None
    now = utcnow()
    rate_limit = controller.state(session, "delivery_after")
    if rate_limit and datetime.fromisoformat(rate_limit) > now:
        return None
    query = select(BotMessage).where(
        BotMessage.dedup_key.startswith(f"{controller.scope}:"),
        BotMessage.status == "pending",
        BotMessage.available_at <= now,
    )
    if controller.state(session, "paused"):
        query = query.where(
            ~BotMessage.dedup_key.startswith(f"{controller.scope}:job:"),
            ~BotMessage.dedup_key.startswith(f"{controller.scope}:reminder:"),
        )
    message = session.scalar(
        query.order_by(
            case((BotMessage.dedup_key.contains(":update:"), 0), else_=1),
            BotMessage.id,
        ).limit(1)
    )
    if message is None:
        return None
    key = message.dedup_key.removeprefix(f"{controller.scope}:").split(":")
    if key[0] in {"job", "reminder"}:
        try:
            job = get_job(session, int(key[1]))
        except ValueError:
            message.status = "cancelled"
            session.commit()
            return None
        if key[0] == "job":
            eligible = (
                job.is_active
                and not job.ignored
                and not job.application
                and (job.match_score >= controller.config["minimum_score"])
            )
            if active_scope() is not None:
                eligible = (
                    eligible
                    and session.scalar(select(Job.id).where(Job.id == job.id, job_filter()))
                    is not None
                )
        else:
            app = job.application
            today = (
                now.replace(tzinfo=UTC).astimezone(ZoneInfo(controller.config["timezone"])).date()
            )
            eligible = bool(
                app
                and app.status not in CLOSED_STATUSES
                and (str(app.next_action_date) == key[2] and str(today) == key[3])
            )
        if not eligible:
            message.status = "cancelled"
            session.commit()
            return None
        card = job_card(job)
        if key[0] == "reminder":
            card["text"] = "<b>Przypomnienie</b>\n" + card["text"]
        message.payload = {**message.payload, **card}
    message.status = "sending"
    message.attempts += 1
    session.commit()
    return message


async def deliver_one(client, sessions, controller: BotController) -> bool:  # noqa: ANN001
    with sessions() as session:
        message = prepare_message(session, controller)
        if message is None:
            return False
        identifier, method, payload = message.id, message.method, message.payload
    status, error, delay = "sent", None, 0
    fallback = False
    try:
        await client.call(method, **payload)
    except TelegramError as exc:
        error = str(exc)[:500]
        if exc.code == 429:
            status, delay = "pending", max(1, exc.retry_after)
        elif method == "editMessageText" and "message is not modified" in error.lower():
            status = "sent"
        elif (
            method == "editMessageText"
            and exc.code == 400
            and ("message to edit not found" in error.lower() or "can't be edited" in error.lower())
        ):
            status, fallback = "pending", True
        else:
            status = "uncertain" if exc.code >= 500 else "failed"
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        status, error, delay = "pending", "Brak połączenia z Telegramem", 30
    except httpx.HTTPError:
        # Telegram has no client idempotency key. A read timeout can follow a successful send.
        status, error = "uncertain", "Brak potwierdzenia doręczenia z Telegrama"
    with sessions() as session:
        message = session.get(BotMessage, identifier)
        message.status, message.error = status, error
        message.available_at = utcnow() + timedelta(seconds=delay)
        if fallback:
            message.method = "sendMessage"
            message.payload = {
                key: value for key, value in message.payload.items() if key != "message_id"
            }
        if delay:
            controller.set_state(session, "delivery_after", message.available_at.isoformat())
        if status == "sent":
            message.sent_at = utcnow()
        if status == "pending" and message.attempts >= 10:
            message.status = "failed"
        session.commit()
    return True
