from __future__ import annotations

# Assertions are the test contract, not production validation.
# ruff: noqa: S101
import uuid

from .conftest import create_business, register


def test_health_and_capabilities_are_public(client):
    assert client.get("/health").status_code == 200
    capabilities = client.get("/api/v1/meta/capabilities")
    assert capabilities.status_code == 200
    body = capabilities.json()
    assert "ai" in body and "connectors" in body


def test_auth_and_business_tenant_boundary(client, user_pair):
    owner, _, stranger, _ = user_pair
    business = create_business(client, owner)
    assert client.get("/api/v1/businesses", headers=owner).status_code == 200
    assert (
        client.get(f"/api/v1/businesses/{business['id']}", headers=stranger).status_code
        == 404
    )
    assert client.get("/api/v1/businesses", headers={}).status_code == 401


def test_logout_invalidates_access_token(client):
    token, _ = register(client, f"logout-{uuid.uuid4().hex}@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 200
    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 401


def test_profile_validation_and_settings_honesty(client, user_pair):
    owner, _, _, _ = user_pair
    business = create_business(client, owner)
    profile = client.patch(
        f"/api/v1/businesses/{business['id']}/profile",
        headers=owner,
        json={"target_audience": "Local customers"},
    )
    assert profile.status_code == 200
    assert "missing_fields" in profile.json()
    settings = client.patch(
        "/api/v1/settings",
        headers=owner,
        json={"timezone": "Asia/Kolkata", "ai_preferences": {"creativity": 0.5}},
    )
    assert settings.status_code == 200
    assert settings.json()["timezone"] == "Asia/Kolkata"
