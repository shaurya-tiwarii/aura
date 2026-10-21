"""APScheduler integration; database rows remain the source of truth.

Scheduler leadership
--------------------
uvicorn runs with multiple worker processes (``--workers 2`` in the image
``CMD``), and every worker executes the application lifespan. Without
coordination each worker would start its own ``BackgroundScheduler`` and every
scheduled post would publish twice.

Leader election uses a Postgres advisory lock, which is the lightest
primitive that is already available (no extra infrastructure, no split-brain
window beyond a crashed leader's TCP timeout):

* On startup each worker tries ``pg_try_advisory_lock`` on a fixed key using
  a dedicated connection that stays open for the life of the process.
* Exactly one worker wins and starts the scheduler; the others skip
  ``scheduler.start()`` and serve API traffic only. Job *registration* is
  deliberately not gated -- adding a job to a scheduler that never starts is
  a harmless no-op, and the database rows (the source of truth) converge via
  the leader's startup and reconcile passes -- so execution, the thing that
  must happen exactly once, is what leadership controls.
* If the leader worker dies, the OS closes its connection, Postgres releases
  the lock, and uvicorn's process supervisor respawns the worker -- the fresh
  process re-runs the lifespan and wins the now-free lock.

With SQLite (local development) there is no advisory lock, so the process
assumes it is alone and starts the scheduler; dev runs a single uvicorn
worker.

Rows scheduled through a *non-leader* worker (e.g. ``POST .../schedule``)
never reach a running scheduler on that worker. The leader therefore runs a
reconciliation tick every few minutes that re-registers every ``SCHEDULED``
row and enabled monitor (``replace_existing=True`` makes this idempotent)
and removes jobs whose rows have gone away. Worst case, a post scheduled via
a standby worker waits for the next tick rather than being silently dropped.
"""

from __future__ import annotations

import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.database import session_scope
from app.core.logging import get_logger
from app.models.enums import (
    ActivityAction,
    MonitorFrequency,
    NotificationLevel,
    NotificationType,
    PostStatus,
    ScanTrigger,
)
from app.models.publishing import ScheduledPost
from app.models.website import Website
from app.services.business import get_profile
from app.services.publishing import publish_existing_post
from app.services.scan import create_scan, dispatch_scan

log = get_logger(__name__)
scheduler = BackgroundScheduler(timezone=settings.scheduler_timezone)

# Fixed advisory-lock key identifying the AURA publishing scheduler.
_SCHEDULER_LOCK_KEY = 2026092501
_RECONCILE_JOB_ID = "scheduler-reconcile"
_RECONCILE_INTERVAL_MINUTES = 5


class _SchedulerLeadership:
    """One-process leader election via a Postgres advisory lock.

    The lock connection is held open for the process lifetime: closing it
    would release the lock while this worker still believes it is the leader.
    A dedicated ``NullPool`` engine is used so the held connection never
    starves the API's main pool.
    """

    def __init__(self) -> None:
        self.is_leader: bool = False
        self._engine: Engine | None = None
        self._connection: Connection | None = None

    def acquire(self) -> bool:
        if settings.is_sqlite:
            # No advisory locks in SQLite; local dev runs a single worker, so
            # the lone process is the leader by definition.
            self.is_leader = True
            log.info("scheduler_leader", mode="sqlite-single-process")
            return True
        try:
            self._engine = create_engine(
                settings.resolved_database_url, poolclass=NullPool
            )
            self._connection = self._engine.connect()
            self.is_leader = bool(
                self._connection.execute(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": _SCHEDULER_LOCK_KEY},
                ).scalar()
            )
        except Exception as exc:  # noqa: BLE001 - election must not kill boot
            log.warning(
                "scheduler_leader_election_failed", error_type=type(exc).__name__
            )
            self.is_leader = False
        if self.is_leader:
            log.info("scheduler_leader", mode="advisory-lock")
        else:
            log.info("scheduler_standby", detail="another worker holds the scheduler lock")
            self.release()
        return self.is_leader

    def release(self) -> None:
        if self._connection is not None:
            # Best effort on shutdown: the OS closes the socket anyway,
            # which releases the advisory lock.
            with suppress(Exception):
                self._connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": _SCHEDULER_LOCK_KEY},
                )
                self._connection.close()
            self._connection = None
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
        self.is_leader = False


_leadership = _SchedulerLeadership()


def is_scheduler_leader() -> bool:
    """Whether this process won the scheduler leadership election."""
    return _leadership.is_leader


def _notify_retry_scheduled(session, item: ScheduledPost) -> None:
    """Tell the owner a scheduled post failed but will be retried."""
    try:
        from app.services import system as system_service

        business = item.post.content.business
        user = business.owner
        retry_at = item.next_retry_at
        system_service.create_notification(
            session,
            user,
            notification_type=NotificationType.SCHEDULE_UPCOMING,
            level=NotificationLevel.WARNING,
            title="Scheduled post will be retried",
            message=(
                f"Publishing failed (attempt {item.retry_count}). "
                f"Next try {retry_at.strftime('%Y-%m-%d %H:%M UTC') if retry_at else 'soon'} "
                f"-- {item.max_retries - item.retry_count} attempt(s) left."
            ),
            business_id=business.id,
            link_path="/dashboard?section=Publishing",
            context={
                "scheduled_post_id": str(item.id),
                "retry_count": item.retry_count,
                "next_retry_at": retry_at.isoformat() if retry_at else None,
            },
        )
        system_service.record_activity(
            session,
            user=user,
            action=ActivityAction.CONTENT_SCHEDULED,
            description=(
                f"Scheduled post failed and was queued for retry "
                f"(attempt {item.retry_count})."
            ),
            business_id=business.id,
            entity_type="scheduled_post",
            entity_id=item.id,
        )
    except Exception:
        log.error(
            "scheduled_retry_notification_failed",
            scheduled_post_id=str(item.id),
            exc_info=True,
        )


def _run_scheduled(scheduled_id: uuid.UUID) -> None:
    """Execute one scheduled post, retrying with exponential backoff.

    The row stays ``SCHEDULED`` while retries remain, so a restart between
    attempts is recovered by the startup reconciliation like any other pending
    post. Only when the retry budget is exhausted does the row become
    ``FAILED``. Publishing is a core workflow, so the first failure must not
    be terminal.
    """
    with session_scope() as session:
        item = session.get(ScheduledPost, scheduled_id)
        if item is None or item.status != PostStatus.SCHEDULED.value:
            return
        item.last_attempt_at = datetime.now(UTC)
        try:
            business = item.post.content.business
            publish_existing_post(session, business, item.post)
            item.status = PostStatus.PUBLISHED.value
            item.completed_at = datetime.now(UTC)
            item.next_retry_at = None
            log.info(
                "scheduled_publish_succeeded",
                scheduled_post_id=str(scheduled_id),
            )
        except Exception as exc:  # noqa: BLE001 - job must persist failure safely
            item.retry_count += 1
            if item.can_retry:
                backoff = settings.scheduler_retry_backoff_seconds * (
                    2 ** (item.retry_count - 1)
                )
                item.next_retry_at = datetime.now(UTC) + timedelta(seconds=backoff)
                item.error_message = (
                    f"Scheduled publishing failed (attempt {item.retry_count}); "
                    "retrying automatically."
                )
                # Re-arm the APScheduler trigger at the backoff time. The row
                # is the source of truth; the job is just the wake-up call.
                register_scheduled(item)
                _notify_retry_scheduled(session, item)
                log.warning(
                    "scheduled_publish_retry_scheduled",
                    scheduled_post_id=str(scheduled_id),
                    retry_count=item.retry_count,
                    next_retry_at=item.next_retry_at.isoformat(),
                    error_type=type(exc).__name__,
                )
            else:
                item.status = PostStatus.FAILED.value
                item.next_retry_at = None
                item.error_message = (
                    "Scheduled publishing failed after "
                    f"{item.retry_count} attempt(s)."
                )
                log.error(
                    "scheduled_publish_failed",
                    scheduled_post_id=str(scheduled_id),
                    error_type=type(exc).__name__,
                )


def _run_monitor(website_id: uuid.UUID) -> None:
    """Create one ordinary scheduled scan and hand it to the normal scan worker."""
    with session_scope() as session:
        website = session.get(Website, website_id)
        if website is None or not website.monitoring_enabled:
            return
        try:
            scan = create_scan(session, website, trigger=ScanTrigger.SCHEDULED)
            session.commit()
            session.refresh(website)
            profile = get_profile(session, website.business)
            keywords = list(profile.keywords) if profile and profile.keywords else None
            website.last_monitor_run_at = datetime.now(UTC)
            session.commit()
            dispatch_scan(scan.id, keywords=keywords)
            log.info("monitor_scan_queued", website_id=str(website_id), scan_id=str(scan.id))
        except Exception as exc:  # noqa: BLE001 - monitor must not kill scheduler
            session.rollback()
            log.warning(
                "monitor_scan_skipped",
                website_id=str(website_id),
                error_type=type(exc).__name__,
            )


def _reconcile() -> None:
    """Leader-only tick: converge live jobs with the database rows.

    Picks up posts/monitors scheduled through a standby worker (whose local
    scheduler never runs) and removes jobs whose rows were cancelled or
    disabled elsewhere. Runs on an interval so the worst case is a few
    minutes of delay, never a silently dropped post.
    """
    try:
        register_pending()
    except Exception as exc:  # noqa: BLE001 - reconcile must not kill scheduler
        log.error("scheduler_reconcile_failed", error_type=type(exc).__name__)
        return
    try:
        with session_scope() as session:
            live_scheduled = {
                row.job_id
                for row in session.scalars(
                    select(ScheduledPost).where(
                        ScheduledPost.status == PostStatus.SCHEDULED.value,
                        ScheduledPost.job_id.is_not(None),
                    )
                )
                if row.job_id
            }
            live_monitors = {
                website.monitor_job_id
                for website in session.scalars(
                    select(Website).where(
                        Website.monitoring_enabled.is_(True),
                        Website.monitor_job_id.is_not(None),
                    )
                )
                if website.monitor_job_id
            }
        for job in scheduler.get_jobs():
            if job.id == _RECONCILE_JOB_ID:
                continue
            if (job.id.startswith("scheduled-post-") and job.id not in live_scheduled) or (job.id.startswith("website-monitor-") and job.id not in live_monitors):
                scheduler.remove_job(job.id)
                log.info("scheduler_removed_stale_job", job_id=job.id)
    except Exception as exc:  # noqa: BLE001 - reconcile must not kill scheduler
        log.error("scheduler_reconcile_cleanup_failed", error_type=type(exc).__name__)


def register_pending() -> int:
    if not settings.scheduler_enabled:
        return 0
    count = 0
    with session_scope() as session:
        rows = list(session.scalars(select(ScheduledPost).where(ScheduledPost.status == PostStatus.SCHEDULED.value)))
        for item in rows:
            register_scheduled(item)
            count += 1
        monitors = list(session.scalars(select(Website).where(Website.monitoring_enabled.is_(True))))
        for website in monitors:
            register_monitor(website)
            count += 1
    return count


def register_scheduled(item: ScheduledPost) -> str | None:
    if not settings.scheduler_enabled:
        return None
    job_id = item.job_id or f"scheduled-post-{item.id}"
    # A retry re-arms at its backoff time; a fresh post at its scheduled time.
    run_date = item.next_retry_at or item.scheduled_for
    # Registration is unconditional: adding a job to a scheduler that never
    # starts (a standby worker) is a harmless no-op, while the leader's
    # startup/reconcile registration converges on the database rows. Only
    # start() is leadership-gated, so jobs can never execute twice.
    scheduler.add_job(
        _run_scheduled,
        DateTrigger(run_date=run_date),
        args=[item.id],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=settings.scheduler_misfire_grace_seconds,
    )
    item.job_id = job_id
    return job_id


def unregister_monitor(website: Website) -> None:
    job_id = website.monitor_job_id
    if not job_id:
        return
    # Already gone is fine.
    with suppress(Exception):
        scheduler.remove_job(job_id)
    website.monitor_job_id = None


def register_monitor(website: Website) -> str | None:
    if not settings.scheduler_enabled or not website.monitoring_enabled:
        return None
    # Remove an earlier registration before replacing the durable row's job id.
    # Already gone is fine.
    if website.monitor_job_id:
        with suppress(Exception):
            scheduler.remove_job(website.monitor_job_id)
    tz = ZoneInfo(website.monitor_timezone or settings.scheduler_timezone)
    day_of_week = "*" if website.monitor_frequency == MonitorFrequency.DAILY.value else str(website.monitor_day_of_week)
    trigger = CronTrigger(
        day_of_week=day_of_week,
        hour=website.monitor_hour,
        minute=website.monitor_minute,
        timezone=tz,
    )
    job_id = f"website-monitor-{website.id}"
    # See register_scheduled: registration is unconditional, execution is
    # leadership-gated via start().
    scheduler.add_job(
        _run_monitor,
        trigger,
        args=[website.id],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=settings.scheduler_misfire_grace_seconds,
    )
    website.monitor_job_id = job_id
    return job_id


def start() -> None:
    if not settings.scheduler_enabled:
        log.info("scheduler_disabled")
        return
    # Exactly one worker may run jobs; the rest serve API traffic only.
    if not _leadership.acquire():
        return
    if scheduler.running:
        return
    try:
        # register_pending() runs before scheduler.start(): APScheduler queues
        # jobs added pre-start, so pending posts and monitors survive a
        # restart via the database rows (the source of truth).
        registered = register_pending()
        log.info("scheduler_pending_registered", count=registered)
    except Exception as exc:  # noqa: BLE001 - startup must not hide API health
        log.error("scheduler_registration_failed", error_type=type(exc).__name__)
    scheduler.add_job(
        _reconcile,
        IntervalTrigger(minutes=_RECONCILE_INTERVAL_MINUTES),
        id=_RECONCILE_JOB_ID,
        replace_existing=True,
    )
    scheduler.start()
    log.info("scheduler_started", leader=True)


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
    _leadership.release()
