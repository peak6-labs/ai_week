from __future__ import annotations
import asyncio
import logging

_log = logging.getLogger("kalshi_trader")


async def with_retry(coroutine_function, *args, attempts: int = 6, **kwargs):
    """Call coroutine_function(*args, **kwargs), retrying on HTTP 429 with exponential backoff.

    Raises the last 429 exception if all attempts are exhausted - never silently
    returns an empty dict, which would cause callers to misread the end-of-data
    sentinel (empty cursor) and stop pagination early.
    """
    last_exception: Exception | None = None
    for attempt in range(attempts):
        try:
            return await coroutine_function(*args, **kwargs)
        except Exception as caught_exception:
            status = getattr(getattr(caught_exception, "response", None), "status_code", None)
            if status == 429:
                delay = 2 ** attempt
                _log.warning(
                    "Rate limited (429); backing off %ds (attempt %d/%d)",
                    delay, attempt + 1, attempts,
                )
                last_exception = caught_exception
                await asyncio.sleep(delay)
            else:
                raise
    raise last_exception  # type: ignore[misc]
