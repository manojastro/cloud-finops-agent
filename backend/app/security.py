from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import Depends, Header, HTTPException, Request


class InMemoryRateLimiter:
    def __init__(self, limit: int):
        self.limit = limit
        self.requests: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        window = self.requests[key]
        while window and window[0] < now - 60:
            window.popleft()
        if len(window) >= self.limit:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        window.append(now)


def actor_context(
    x_actor: str = Header(default="local-reviewer", min_length=2, max_length=80),
    x_role: str = Header(default="viewer", min_length=2, max_length=30),
) -> tuple[str, str]:
    return x_actor, x_role.lower()


def require_approver(context: tuple[str, str]) -> tuple[str, str]:
    actor, role = context
    if role not in {"approver", "admin"}:
        raise HTTPException(status_code=403, detail="Approver or admin role required")
    return actor, role


def approver_context(context: tuple[str, str] = Depends(actor_context)) -> tuple[str, str]:
    return require_approver(context)


async def rate_limit(request: Request) -> None:
    request.app.state.rate_limiter.check(request.client.host if request.client else "local")
