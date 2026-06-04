from __future__ import annotations
import asyncio


async def with_retry(coroutine_function, *args, attempts: int = 4,
                     raise_on_exhaust: bool = False, **kwargs):
    """Call coroutine_function(*args, **kwargs), retrying on HTTP 429 with exponential backoff.

    On a persistent 429 across all attempts this returns ``{}`` by default — read
    paths tolerate "no data". Write paths (order placement) should pass
    ``raise_on_exhaust=True`` so an undelivered request surfaces as an error rather
    than a silent empty dict that looks like a successful no-op.
    """
    last_exception: Exception | None = None
    for attempt in range(attempts):
        try:
            return await coroutine_function(*args, **kwargs)
        except Exception as caught_exception:
            status = getattr(getattr(caught_exception, "response", None), "status_code", None)
            if status == 429:
                last_exception = caught_exception
                await asyncio.sleep(2 ** attempt)
            else:
                raise
    if raise_on_exhaust and last_exception is not None:
        raise last_exception
    return {}
