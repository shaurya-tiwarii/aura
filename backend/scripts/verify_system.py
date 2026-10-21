"""Focused verification for system APIs and preference validation.

Run from ``backend/`` with the project's Python interpreter.
"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.models import Base, Notification, User  # noqa: E402
from app.models.enums import NotificationLevel, NotificationType  # noqa: E402
from app.schemas.system import SettingsUpdate  # noqa: E402
from app.services import system as system_service  # noqa: E402


def main() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        user = User(email="system-check@example.com", password_hash="fixture")
        session.add(user)
        session.flush()
        notification = system_service.create_notification(
            session,
            user,
            notification_type=NotificationType.SCAN_COMPLETED,
            level=NotificationLevel.SUCCESS,
            title="Scan complete",
            message="Your scan is ready.",
        )
        session.commit()
        assert notification is not None
        items, total, unread = system_service.list_notifications(session, user)
        assert len(items) == total == unread == 1
        system_service.mark_read(items[0])
        session.commit()
        assert system_service.list_notifications(session, user)[2] == 0

        update = SettingsUpdate(
            theme="dark",
            timezone="Asia/Kolkata",
            notification_preferences={"scan_completed": False},
            ai_preferences={"creativity": 0.5, "length": "short"},
        )
        system_service.update_settings(session, user, update)
        session.commit()
        assert user.theme == "dark" and user.timezone == "Asia/Kolkata"
        assert user.notification_enabled("scan_completed") is False
        assert (
            system_service.create_notification(
                session,
                user,
                notification_type=NotificationType.SCAN_COMPLETED,
                level=NotificationLevel.INFO,
                title="Suppressed",
                message="Not created when opted out.",
            )
            is None
        )
        assert session.query(Notification).count() == 1
        try:
            SettingsUpdate(notification_preferences={"not_a_type": False})
        except ValueError:
            pass
        else:
            raise AssertionError("unknown notification preference was accepted")
    print("system verification passed")


if __name__ == "__main__":
    main()
