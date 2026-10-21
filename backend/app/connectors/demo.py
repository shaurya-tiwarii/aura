"""Explicit no-network demo connector."""

from __future__ import annotations

from app.connectors.base import PublishResult


class DemoConnector:
    def __init__(self, platform: str) -> None:
        self.platform = platform
        self.demo = True

    def publish(
        self, *, title: str, body: str, target_identifier: str | None
    ) -> PublishResult:
        return PublishResult(
            success=True,
            response_payload={
                "demo": True,
                "platform": self.platform,
                "message": "No remote request was made; this is a demo publish.",
                "title_length": len(title),
                "body_length": len(body),
            },
        )
