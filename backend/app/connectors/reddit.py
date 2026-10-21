"""Reddit connector using the submit endpoint."""

from __future__ import annotations

import httpx

from app.connectors.base import ConnectorError, PublishResult


class RedditConnector:
    platform = "reddit"
    demo = False

    def __init__(self, token: str, user_agent: str) -> None:
        self.token, self.user_agent = token, user_agent

    def publish(
        self, *, title: str, body: str, target_identifier: str | None
    ) -> PublishResult:
        if not target_identifier:
            raise ConnectorError("Reddit requires a subreddit identifier.")
        try:
            response = httpx.post(
                "https://oauth.reddit.com/api/submit",
                headers={
                    "Authorization": f"bearer {self.token}",
                    "User-Agent": self.user_agent,
                },
                data={
                    "sr": target_identifier,
                    "title": title,
                    "text": body,
                    "kind": "self",
                },
                timeout=20,
            )
        except httpx.HTTPError as exc:
            raise ConnectorError("Reddit could not be reached.") from exc
        if response.status_code >= 400:
            raise ConnectorError("Reddit rejected the post.")
        data = response.json().get("json", {}).get("data", {})
        return PublishResult(
            True, data.get("id"), data.get("url"), {"status_code": response.status_code}
        )
