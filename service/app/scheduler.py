"""In-process scheduled jobs (APScheduler). One job: the weekly bring list."""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .agentmail import AgentMail, AgentMailError
from .config import settings
from .db import SessionLocal
from .emails import bring_list_email
from .orders import unpicked_by_size

log = logging.getLogger(__name__)


def send_bring_list() -> str:
    from sqlalchemy import func, select

    from .db import Order

    with SessionLocal() as session:
        rows, pending = unpicked_by_size(session)
        awaiting = session.scalar(select(func.count()).select_from(Order).where(Order.status == "needs_email")) or 0
    subject, text = bring_list_email(rows, pending, awaiting)
    if not settings.agentmail_api_key:
        log.info("bring list (no AgentMail key):\n%s", text)
        return text
    try:
        AgentMail().send(to=[settings.org_email], subject=subject, text=text, labels=["bring-list"])
    except AgentMailError as exc:
        log.error("bring list email failed: %s", exc)
    return text


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=settings.timezone)
    if settings.bring_list_day and settings.bring_list_time:
        hour, minute = settings.bring_list_time.split(":")
        scheduler.add_job(
            send_bring_list,
            CronTrigger(day_of_week=settings.bring_list_day.lower()[:3], hour=int(hour), minute=int(minute)),
            id="bring_list",
            replace_existing=True,
        )
        log.info("bring list scheduled for %s %s %s", settings.bring_list_day, settings.bring_list_time, settings.timezone)
    if settings.poll_interval_seconds > 0 and settings.agentmail_api_key:
        from .poller import poll_inbox

        scheduler.add_job(poll_inbox, "interval", seconds=settings.poll_interval_seconds, id="poll_inbox", replace_existing=True, max_instances=1, coalesce=True)
        log.info("inbox polling every %ss", settings.poll_interval_seconds)
    scheduler.start()
    return scheduler
