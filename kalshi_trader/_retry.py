from __future__ import annotations
import asyncio


async def with_retry(coro_fn, *args, attempts: int = 4, **kwargs):
    """Call coro_fn(*args, **kwargs), retrying on HTTP 429 with exponential backoff."""
    for attempt in range(attempts):
        try:
            return await coro_fn(*args, **kwargs)
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 429:
                await asyncio.sleep(2 ** attempt)
            else:
                raise
    return {}
