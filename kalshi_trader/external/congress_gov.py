"""congress.gov committee-meeting client — hearing schedule for the mentions veto.

A "Will <person> say <word> in a hearing" market is moot if the hearing was
canceled or postponed past the market's close, or was never on the calendar. The
Library of Congress congress.gov API exposes committee meetings with a
``meetingStatus`` (Scheduled / Canceled / Postponed / Rescheduled). One free
``api.data.gov`` key (:data:`kalshi_trader.config.DATA_GOV_API_KEY`) authorizes it;
without the key the client returns ``[]`` and the system degrades gracefully (no
schedule signal).

Network fetch and parsing are separated: :func:`parse_committee_meeting` and
:func:`normalize_meeting_status` are pure and tested on fixtures; the client
orchestrates the list + per-meeting detail fetches fail-soft.

API docs: https://github.com/LibraryOfCongress/api.congress.gov/blob/main/Documentation/CommitteeMeetingEndpoint.md
"""
from __future__ import annotations

import ssl
from datetime import datetime
from urllib.parse import urlencode

import aiohttp

from kalshi_trader import config

CONGRESS_GOV_BASE = "https://api.congress.gov/v3"
_HEADERS = {"User-Agent": "kalshi-trader/1.0", "Accept": "application/json"}

# Canonical meeting statuses. The API has used both "Canceled" and "Cancelled";
# fold to one canonical spelling.
STATUS_SCHEDULED = "Scheduled"
STATUS_CANCELED = "Canceled"
STATUS_POSTPONED = "Postponed"
STATUS_RESCHEDULED = "Rescheduled"

# A disrupted status means a scheduled hearing will not happen as planned.
DISRUPTED_STATUSES = frozenset({STATUS_CANCELED, STATUS_POSTPONED, STATUS_RESCHEDULED})

_STATUS_ALIASES = {
    "scheduled": STATUS_SCHEDULED,
    "canceled": STATUS_CANCELED,
    "cancelled": STATUS_CANCELED,
    "postponed": STATUS_POSTPONED,
    "rescheduled": STATUS_RESCHEDULED,
}


def _build_ssl_context() -> ssl.SSLContext:
    """SSL context trusting the OS store (corporate-proxy safe; see gdelt.py)."""
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # pragma: no cover - truststore optional
        return ssl.create_default_context()


def normalize_meeting_status(raw: str | None) -> str:
    """Map a raw ``meetingStatus`` to a canonical status.

    Unknown/blank values fall back to ``Scheduled`` (the API's default for a
    meeting still on the calendar), so an unrecognized status never fabricates a
    veto on its own.
    """
    if not raw:
        return STATUS_SCHEDULED
    return _STATUS_ALIASES.get(str(raw).strip().lower(), STATUS_SCHEDULED)


def _event_date(raw_date: str | None) -> str:
    """``YYYY-MM-DD`` from an ISO meeting date, else ""."""
    if not raw_date:
        return ""
    try:
        return datetime.fromisoformat(str(raw_date).replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return str(raw_date)[:10]


def parse_committee_meeting(detail_json: dict) -> dict | None:
    """Parse a committee-meeting *detail* response into a schedule record.

    Accepts the ``{"committeeMeeting": {...}}`` shape. Returns
    ``{meeting_id, committee, chamber, event_date, status, title}`` or None when
    the payload has no usable meeting.
    """
    meeting = (detail_json or {}).get("committeeMeeting")
    if not isinstance(meeting, dict):
        return None
    committees = meeting.get("committees") or []
    committee_name = ""
    if committees and isinstance(committees[0], dict):
        committee_name = str(committees[0].get("name") or "")
    return {
        "meeting_id": str(meeting.get("eventId") or ""),
        "committee": committee_name,
        "chamber": str(meeting.get("chamber") or ""),
        "event_date": _event_date(meeting.get("date")),
        "status": normalize_meeting_status(meeting.get("meetingStatus")),
        "title": str(meeting.get("title") or ""),
    }


def parse_meeting_list(list_json: dict) -> list[dict]:
    """Pull ``{eventId, chamber}`` stubs from a committee-meeting *list* response."""
    stubs: list[dict] = []
    for meeting in (list_json or {}).get("committeeMeetings", []) or []:
        if not isinstance(meeting, dict):
            continue
        event_id = str(meeting.get("eventId") or "")
        if event_id:
            stubs.append({"eventId": event_id, "chamber": str(meeting.get("chamber") or "")})
    return stubs


class CongressGovClient:
    """Async client for the congress.gov committee-meeting endpoint (fail-soft).

    Returns ``[]`` immediately when no ``api.data.gov`` key is configured.
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key if api_key is not None else config.DATA_GOV_API_KEY
        self._session: aiohttp.ClientSession | None = None

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            connector = aiohttp.TCPConnector(ssl=_build_ssl_context())
            self._session = aiohttp.ClientSession(headers=_HEADERS, connector=connector)
        return self._session

    async def _get_json(self, url: str, params: dict) -> dict:
        session = self._ensure_session()
        query = {**params, "api_key": self._api_key, "format": "json"}
        async with session.get(
            f"{url}?{urlencode(query)}", timeout=aiohttp.ClientTimeout(total=30)
        ) as api_response:
            api_response.raise_for_status()
            return await api_response.json()

    async def get_committee_meetings(
        self, congress: int, chamber: str, limit: int = 50
    ) -> list[dict]:
        """List recent committee meetings and resolve each to a schedule record.

        Makes one list call plus one detail call per meeting (the list response
        carries only stubs). Every call is fail-soft: a failure drops that meeting
        rather than aborting. Returns ``[]`` when no API key is configured.
        """
        if not self._api_key:
            return []
        try:
            list_json = await self._get_json(
                f"{CONGRESS_GOV_BASE}/committee-meeting/{congress}/{chamber}", {"limit": limit}
            )
        except (aiohttp.ClientError, OSError):
            return []

        records: list[dict] = []
        for stub in parse_meeting_list(list_json):
            try:
                detail = await self._get_json(
                    f"{CONGRESS_GOV_BASE}/committee-meeting/{congress}/{chamber}/{stub['eventId']}",
                    {},
                )
            except (aiohttp.ClientError, OSError):
                continue
            record = parse_committee_meeting(detail)
            if record:
                records.append(record)
        return records

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
