"""HTTP layer: routers and the dependencies they share.

Nothing in here contains business logic. Routes validate input, call into
``app.auth`` / ``app.services`` / ``app.engines``, and shape the response; the
work itself lives behind those interfaces so it stays testable without HTTP.
"""

from __future__ import annotations
