"""Common connector contract.

Connectors never receive ORM objects or return credentials. Demo connectors
perform no network request and deliberately leave remote identifiers empty.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class ConnectorError(RuntimeError):
    """A safe, user-facing connector failure."""


@dataclass(frozen=True)
class PublishResult:
    success: bool
    remote_post_id: str | None = None
    remote_url: str | None = None
    response_payload: dict[str, object] = field(default_factory=dict)
    error_message: str | None = None


class Connector(Protocol):
    platform: str
    demo: bool

    def publish(
        self, *, title: str, body: str, target_identifier: str | None
    ) -> PublishResult: ...
