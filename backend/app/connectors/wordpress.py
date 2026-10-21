"""WordPress connector.

The live path uses the site's REST API and a bearer token supplied by the
publishing service. OAuth acquisition is intentionally outside this adapter.
"""

from __future__ import annotations

import httpx

from app.connectors.base import ConnectorError, PublishResult


class WordPressConnector:
    platform = "wordpress"
    demo = False

    def __init__(self, token: str, site_url: str) -> None:
        self.token, self.site_url = token, site_url.rstrip("/")

    def publish(
        self, *, title: str, body: str, target_identifier: str | None
    ) -> PublishResult:
        endpoint = f"{self.site_url}/wp-json/wp/v2/posts"
        try:
            response = httpx.post(
                endpoint,
                headers={"Authorization": f"Bearer {self.token}"},
                json={"title": title, "content": body, "status": "publish"},
                timeout=20,
            )
        except httpx.HTTPError as exc:
            raise ConnectorError("WordPress could not be reached.") from exc
        if response.status_code >= 400:
            raise ConnectorError("WordPress rejected the post.")
        data = response.json()
        return PublishResult(
            True,
            str(data.get("id")) if data.get("id") is not None else None,
            data.get("link"),
            {"status_code": response.status_code},
        )
