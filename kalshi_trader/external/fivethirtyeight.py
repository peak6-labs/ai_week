"""Async client for FiveThirtyEight polling CSVs.

538 publishes raw individual-poll CSVs (president, senate, house, governor,
generic ballot) with a pollster-quality grade per row. ABC folded 538's old
``projects.fivethirtyeight.com`` standalone feed into abcnews.com, so the
canonical projects URLs now redirect to an HTML page; the simonw/fivethirtyeight
GitHub mirror tracks the same CC-BY-licensed files with the identical schema and
stable raw URLs, and is used here as the primary source.

We aggregate recent polls for a race → average margin between the two leading
candidates → win probability via a normal model around the margin (the same
scipy.stats.norm-around-a-margin pattern used in signals/weather.py).

Free, no API key, plain CSV over HTTPS. See docs/research/non_financial_sources.md
(source #3).
"""
from __future__ import annotations

import csv
import io
import ssl

import aiohttp

# simonw/fivethirtyeight-polls mirrors the live 538 CSVs (canonical schema,
# CC-BY). One entry per poll family the elections signal supports.
FIVETHIRTYEIGHT_BASE = "https://raw.githubusercontent.com/simonw/fivethirtyeight-polls/main"
POLL_FILES: dict[str, str] = {
    "president": "president_polls.csv",
    "senate": "senate_polls.csv",
    "house": "house_polls.csv",
    "governor": "governor_polls.csv",
    "generic_ballot": "generic_ballot_polls.csv",
}
_HEADERS = {"User-Agent": "kalshi-trader/1.0 scorley@peak6.com", "Accept": "text/csv"}


def _build_ssl_context() -> ssl.SSLContext:
    """SSL context that trusts the OS trust store.

    Behind the corporate proxy (Zscaler) the upstream cert chain ends in a
    self-signed root that only lives in the system trust store, not certifi's
    bundle — so a default aiohttp context fails verification. truststore reads
    the OS store and fixes this (same reason db.py injects truststore for httpx).
    Falls back to the default context if truststore is unavailable.
    """
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # pragma: no cover - truststore optional
        return ssl.create_default_context()


class FiveThirtyEightClient:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def _get_text(self, url: str) -> str:
        if self._session is None:
            connector = aiohttp.TCPConnector(ssl=_build_ssl_context())
            self._session = aiohttp.ClientSession(headers=_HEADERS, connector=connector)
        async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as api_response:
            api_response.raise_for_status()
            return await api_response.text()

    async def get_polls(self, poll_type: str) -> list[dict]:
        """Fetch and parse one 538 polls CSV into a list of row dicts.

        Args:
            poll_type: One of POLL_FILES keys (president, senate, house,
                governor, generic_ballot).

        Returns:
            List of poll-row dicts (csv.DictReader rows). Empty list if the
            poll_type is unknown.
        """
        filename = POLL_FILES.get(poll_type)
        if filename is None:
            return []
        csv_text = await self._get_text(f"{FIVETHIRTYEIGHT_BASE}/{filename}")
        return parse_polls_csv(csv_text)

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None


def parse_polls_csv(csv_text: str) -> list[dict]:
    """Parse a 538 polls CSV string into a list of row dicts."""
    reader = csv.DictReader(io.StringIO(csv_text))
    return [row for row in reader]
