from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

# Assertions are the test contract, not production validation.
# ruff: noqa: S101
from .conftest import create_business


def test_analytics_demo_provenance_and_snapshots(client, user_pair, db_session):
    owner, _, stranger, _ = user_pair
    business = create_business(client, owner)
    content = client.post(
        f"/api/v1/businesses/{business['id']}/content",
        headers=owner,
        json={"content_type": "blog_article", "title": "A post", "body": "Hello"},
    )
    assert content.status_code == 201, content.text
    post_id = content.json()["id"]
    from app.models.publishing import Post

    post = Post(content_id=UUID(post_id), platform="wordpress", mode="demo")
    db_session.add(post)
    db_session.commit()
    post_id = str(post.id)
    payload = {
        "post_id": post_id,
        "platform": "wordpress",
        "measured_on": str(datetime.now(UTC).date()),
        "source": "demo",
        "metrics": {"reach": 12, "likes": 3},
        "raw_payload": {"demo": True},
    }
    response = client.post(
        f"/api/v1/businesses/{business['id']}/analytics/metrics",
        headers=owner,
        json=payload,
    )
    assert response.status_code == 201
    assert response.json()["demo"] is True
    assert all(item["source"] == "demo" for item in response.json()["metrics"])
    assert (
        client.get(
            f"/api/v1/businesses/{business['id']}/analytics/metrics", headers=stranger
        ).status_code
        == 404
    )
    snapshots = client.post(
        f"/api/v1/businesses/{business['id']}/analytics/snapshots/rebuild",
        headers=owner,
    )
    assert snapshots.status_code == 200
    assert snapshots.json()[0]["has_demo_data"] is True


def test_assistant_demo_is_grounded_and_isolated(client, user_pair, monkeypatch):
    owner, _, stranger, _ = user_pair
    business = create_business(client, owner)
    from app.services import assistant as assistant_service
    from app.services.gemini import GeminiService

    demo = GeminiService()
    demo.enabled = False
    monkeypatch.setattr(assistant_service, "GeminiService", lambda: demo)
    response = client.post(
        f"/api/v1/businesses/{business['id']}/assistant/chat",
        headers=owner,
        json={"content": "What should I improve?"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "demo"
    assert "Demo assistant" in body["content"]
    assert body["grounding"]["business"]["id"] == business["id"]
    assert (
        client.get(
            f"/api/v1/businesses/{business['id']}/assistant/conversations",
            headers=stranger,
        ).status_code
        == 404
    )


def test_system_notifications_settings_and_activity_are_account_scoped(
    client, user_pair
):
    owner, _, stranger, _ = user_pair
    business = create_business(client, owner)
    assert client.get("/api/v1/notifications", headers=owner).json()["total"] == 0
    update = client.patch(
        "/api/v1/settings",
        headers=owner,
        json={
            "theme": "dark",
            "notification_preferences": {"scan_completed": False},
        },
    )
    assert update.status_code == 200
    assert update.json()["theme"] == "dark"
    assert client.get("/api/v1/activity", headers=stranger).json()["total"] == 0
    assert business["id"] not in str(
        client.get("/api/v1/activity", headers=owner).json()
    )


def test_improvement_endpoints_keep_website_and_scan_ids_tenant_scoped(
    client, user_pair
):
    owner, _, stranger, _ = user_pair
    business = create_business(client, owner)
    website_id = business["websites"][0]["id"]
    payload = {
        "previous_scan_id": "00000000-0000-0000-0000-000000000001",
        "current_scan_id": "00000000-0000-0000-0000-000000000002",
    }
    path = f"/api/v1/businesses/{business['id']}/websites/{website_id}/improvements"
    assert client.post(path, headers=stranger, json=payload).status_code == 404
    # Owned access reaches validation rather than leaking a cross-tenant row.
    assert client.post(path, headers=owner, json=payload).status_code in {404, 409}


def test_scheduling_registers_a_future_post_without_waiting_for_restart(
    client, user_pair
):
    """The durable row and live scheduler job are created in the same workflow."""
    owner, _, _, _ = user_pair
    business = create_business(client, owner)
    content = client.post(
        f"/api/v1/businesses/{business['id']}/content",
        headers=owner,
        json={"content_type": "blog_article", "title": "Scheduled", "body": "Body"},
    )
    assert content.status_code == 201, content.text
    connection = client.post(
        f"/api/v1/businesses/{business['id']}/publishing/connections",
        headers=owner,
        json={"platform": "wordpress", "account_url": "https://example.com"},
    )
    assert connection.status_code == 201, connection.text
    scheduled_for = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    response = client.post(
        f"/api/v1/businesses/{business['id']}/publishing/posts/schedule",
        headers=owner,
        json={
            "content_id": content.json()["id"],
            "connection_id": connection.json()["id"],
            "scheduled_for": scheduled_for,
            "timezone_name": "UTC",
        },
    )
    assert response.status_code == 201, response.text
    job_id = response.json()["job_id"]
    assert job_id, response.json()
    from app.scheduler.service import scheduler

    try:
        assert scheduler.get_job(job_id) is not None
    finally:
        scheduler.remove_job(job_id)
