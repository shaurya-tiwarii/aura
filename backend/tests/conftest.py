"""Shared isolated API fixtures.

The suite deliberately uses the same SQLite fallback as local development.  No
network or external credentials are needed, and the real authentication and
tenant dependencies remain in the request path.
"""
# Assertions are the test contract, not production validation.
# ruff: noqa: S101

from __future__ import annotations

import os
import uuid

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("AUTH_PROVIDER", "local")
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-long-enough-123456")
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", "")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import get_db
from app.main import create_app
from app.models import Base


@pytest.fixture(scope="session")
def db_engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture()
def client(db_engine):
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)

    def override_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app = create_app()
    app.dependency_overrides[get_db] = override_db
    # Do not enter the context manager: lifespan starts the real scheduler and
    # checks the module-level production engine. Requests do not need startup.
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def db_session(db_engine):
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()


def register(client: TestClient, email: str) -> tuple[str, str]:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "A-secure-password-123", "full_name": email},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["tokens"]["access_token"], body["user"]["id"]


@pytest.fixture()
def user_pair(client):
    suffix = uuid.uuid4().hex
    first_token, first_id = register(client, f"first-{suffix}@example.com")
    second_token, second_id = register(client, f"second-{suffix}@example.com")
    return (
        {"Authorization": f"Bearer {first_token}"},
        first_id,
        {"Authorization": f"Bearer {second_token}"},
        second_id,
    )


def create_business(
    client: TestClient, headers: dict[str, str], url="https://example.com"
):
    response = client.post(
        "/api/v1/businesses",
        headers=headers,
        json={"name": "Test business", "primary_website_url": url},
    )
    assert response.status_code == 201, response.text
    return response.json()
