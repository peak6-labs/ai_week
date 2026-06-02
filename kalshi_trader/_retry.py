from __future__ import annotations
import asyncio


async def with_retry(coroutine_function, *args, attempts: int = 4, **kwargs):
    """Call coroutine_function(*args, **kwargs), retrying on HTTP 429 with exponential backoff."""
    for attempt in range(attempts):
        try:
            return await coroutine_function(*args, **kwargs)
        except Exception as caught_exception:
            status = getattr(getattr(caught_exception, "response", None), "status_code", None)
            if status == 429:
                await asyncio.sleep(2 ** attempt)
            else:
                raise
    return {}
