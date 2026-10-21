"""Focused proof for tenant-safe, grounded assistant conversations.

Run from ``backend/`` with ``.venv/Scripts/python.exe scripts/verify_assistant.py``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["AUTH_PROVIDER"] = "local"
os.environ["APP_ENV"] = "development"
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.core.database import SessionLocal, engine  # noqa: E402
from app.models.base import Base  # noqa: E402
from app.models.business import Business  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.assistant import (  # noqa: E402
    add_message,
    create_conversation,
    get_conversation,
    list_conversations,
)
from app.services.gemini import GeminiService  # noqa: E402


def main() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        owner = User(email="owner@example.com", password_hash="hash")
        stranger = User(email="stranger@example.com", password_hash="hash")
        first = Business(name="Owner business", owner=owner)
        other = Business(name="Other business", owner=stranger)
        session.add_all([owner, stranger, first, other])
        session.commit()

        conversation = create_conversation(session, first, owner.id)
        demo_gemini = GeminiService()
        demo_gemini.enabled = False
        message = add_message(
            session,
            conversation,
            "What should I improve?",
            first,
            gemini=demo_gemini,
        )
        session.commit()
        assert message.model == "demo" and "Demo assistant" in message.content
        assert message.grounding["business"]["id"] == str(first.id)
        assert list_conversations(session, first, owner.id)[0].id == conversation.id
        assert list_conversations(session, other, stranger.id) == []
        try:
            get_conversation(session, first, stranger.id, conversation.id)
        except Exception as exc:  # noqa: BLE001 - exact public boundary is checked
            assert type(exc).__name__ == "NotFoundError"
        else:
            raise AssertionError("conversation crossed the user boundary")
    print("5 assistant checks passed")


if __name__ == "__main__":
    main()
