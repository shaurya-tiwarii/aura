"""LinkedIn connector using the v2 UGC posts endpoint."""

from __future__ import annotations

import httpx

from app.connectors.base import ConnectorError, PublishResult


class LinkedInConnector:
    platform = "linkedin"
    demo = False

    def __init__(self, token: str) -> None:
        self.token = token

    def publish(
        self, *, title: str, body: str, target_identifier: str | None
    ) -> PublishResult:
        if not target_identifier:
            raise ConnectorError(
                "LinkedIn requires an author or organisation identifier."
            )
        try:
            response = httpx.post(
                "https://api.linkedin.com/v2/ugcPosts",
                headers={"Authorization": f"Bearer {self.token}"},
                json={
                    "author": target_identifier,
                    "lifecycleState": "PUBLISHED",
                    "specificContent": {
                        "com.linkedin.ugc.ShareContent": {
                            "shareCommentary": {"text": f"{title}\n\n{body}"},
                            "shareMediaCategory": "NONE",
                        }
                    },
                    "visibility": {
                        "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
                    },
                },
                timeout=20,
            )
        except httpx.HTTPError as exc:
            raise ConnectorError("LinkedIn could not be reached.") from exc
        if response.status_code >= 400:
            raise ConnectorError("LinkedIn rejected the post.")
        return PublishResult(
            True,
            response.headers.get("x-restli-id"),
            response_payload={"status_code": response.status_code},
        )
