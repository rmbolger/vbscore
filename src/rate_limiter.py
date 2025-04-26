"""Simple rate limiter"""

import time
import asyncio
from functools import wraps
from fastapi import Request
from fastapi.responses import JSONResponse

# In-memory store for request timestamps
_request_tracker = {}
_tracker_lock = asyncio.Lock()

def ip_only_key(request: Request):
    """Uses the client IP as the key for rate limiting."""
    return request.client.host


def ip_useragent_key(request: Request):
    """Uses the combination of client IP and user-agent string as the key for rate limiting."""
    ua = request.headers.get("user-agent", "")
    return f"{request.client.host}:{ua}"


def rate_limit(
    max_requests: int,
    window_seconds: int = 3600,
    key_func=ip_only_key,
    error_message: str = "Rate limit exceeded. Try again later."
):
    """
    Rate limit decorator.

    Args:
        max_requests: Max number of allowed requests.
        window_seconds: Time window in seconds.
        key_func: Optional function to extract key from Request. Defaults to ip_only_key
        error_message: Message to return when rate limited.
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            request: Request = kwargs.get('request')
            if not request:
                for arg in args:
                    if isinstance(arg, Request):
                        request = arg
                        break
            if not request:
                raise RuntimeError("Request object not found for rate limiting.")

            client_key = key_func(request)
            current_time = time.time()

            async with _tracker_lock:
                _request_tracker.setdefault(client_key, [])
                _request_tracker[client_key] = [
                    t for t in _request_tracker[client_key] if (current_time - t) < window_seconds
                ]

                if len(_request_tracker[client_key]) >= max_requests:
                    oldest = min(_request_tracker[client_key])
                    retry_after = int(window_seconds - (current_time - oldest))
                    return JSONResponse(
                        status_code=429,
                        content={"detail": error_message},
                        headers={"Retry-After": str(retry_after)}
                    )

                _request_tracker[client_key].append(current_time)

            return await func(*args, **kwargs)

        return wrapper
    return decorator


async def cleanup_request_tracker(expiry_seconds: int = 3600):
    """
    Clean up old entries in _request_tracker.
    Should be called periodically as a background task.
    """
    current_time = time.time()
    async with _tracker_lock:
        keys_to_delete = []
        for key, timestamps in _request_tracker.items():
            recent = [t for t in timestamps if (current_time - t) < expiry_seconds]
            if recent:
                _request_tracker[key] = recent
            else:
                keys_to_delete.append(key)

        for key in keys_to_delete:
            del _request_tracker[key]


async def rate_limiter_cleanup_loop(interval_seconds: int = 600):
    """Background task to periodically clean up request tracker."""
    try:
        while True:
            await asyncio.sleep(interval_seconds)
            await cleanup_request_tracker()
    except asyncio.CancelledError:
        pass  # Shutdown cleanly
