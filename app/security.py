import asyncio
from collections import defaultdict, deque
import secrets
import time

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings, get_settings

bearer_scheme = HTTPBearer(auto_error=False)


class InMemoryRateLimiter:
    """Small single-process rate limiter for the two write endpoints."""

    def __init__(self) -> None:
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(self, client_ip: str, limit: int, window_seconds: int) -> None:
        now = time.monotonic()
        threshold = now - window_seconds
        async with self._lock:
            requests = self._requests[client_ip]
            while requests and requests[0] <= threshold:
                requests.popleft()
            if len(requests) >= limit:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many requests. Try again later.",
                    headers={"Retry-After": str(window_seconds)},
                )
            requests.append(now)


rate_limiter = InMemoryRateLimiter()


async def require_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> None:
    """Require a server-held bearer key before spending Lovable credits."""
    if not settings.app_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Server API authentication is not configured.",
        )
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not secrets.compare_digest(credentials.credentials, settings.app_api_key)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid bearer API key is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    client_ip = request.client.host if request.client else "unknown"
    await rate_limiter.check(client_ip, settings.write_rate_limit, settings.rate_limit_window_seconds)
