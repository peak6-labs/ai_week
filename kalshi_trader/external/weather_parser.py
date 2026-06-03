from __future__ import annotations
import re
from datetime import date, datetime

CITY_COORDS: dict[str, tuple[float, float]] = {
    "new york city": (40.7128, -74.0060),
    "new york": (40.7128, -74.0060),
    "nyc": (40.7128, -74.0060),
    "los angeles": (34.0522, -118.2437),
    "chicago": (41.8781, -87.6298),
    "houston": (29.7604, -95.3698),
    "phoenix": (33.4484, -112.0740),
    "philadelphia": (39.9526, -75.1652),
    "san antonio": (29.4241, -98.4936),
    "san diego": (32.7157, -117.1611),
    "dallas": (32.7767, -96.7970),
    "miami": (25.7617, -80.1918),
    "seattle": (47.6062, -122.3321),
    "boston": (42.3601, -71.0589),
    "denver": (39.7392, -104.9903),
    "atlanta": (33.7490, -84.3880),
    "minneapolis": (44.9778, -93.2650),
    "las vegas": (36.1699, -115.1398),
    "portland": (45.5051, -122.6750),
    "nashville": (36.1627, -86.7816),
    "san francisco": (37.7749, -122.4194),
    "austin": (30.2672, -97.7431),
    "washington": (38.9072, -77.0369),
    "oklahoma city": (35.4676, -97.5164),
}

# Kalshi encodes the city in the *ticker* (e.g. KXLOWTBOS = low-temp Boston),
# not always in the title. Map the ticker city code → CITY_COORDS key. Used as a
# fallback when the title has no recognizable city name.
TICKER_CITY_CODES: dict[str, str] = {
    "NYC": "new york", "NY": "new york",
    "LAX": "los angeles", "LA": "los angeles",
    "CHI": "chicago", "MIA": "miami", "HOU": "houston", "PHIL": "philadelphia",
    "PHX": "phoenix", "PHO": "phoenix",
    "DAL": "dallas", "SEA": "seattle", "BOS": "boston", "DEN": "denver",
    "ATL": "atlanta", "MSP": "minneapolis", "MIN": "minneapolis",
    "LV": "las vegas", "LAS": "las vegas",
    "POR": "portland", "PDX": "portland", "NAS": "nashville",
    "SFO": "san francisco", "SF": "san francisco",
    "AUS": "austin", "DC": "washington", "DCA": "washington",
    "OKC": "oklahoma city", "OKL": "oklahoma city",
}

# Ticker metric prefixes (after the leading KX), longest first so HIGHT beats HIGH.
_TICKER_METRIC_PREFIXES: list[tuple[str, str]] = [
    ("LOWTEMP", "temp_low"), ("HIGHTEMP", "temp_high"),
    ("LOWT", "temp_low"), ("HIGHT", "temp_high"),
    ("LOW", "temp_low"), ("HIGH", "temp_high"),
    ("RAIN", "precipitation"), ("WIND", "wind"),
]


def _city_from_ticker(ticker: str) -> tuple[str, float, float] | None:
    """Extract (city, lat, lon) from a Kalshi weather ticker's first segment.

    Strips the leading ``KX`` and a known metric prefix, then matches the
    remaining leading letters against TICKER_CITY_CODES (longest code first).
    """
    head = ticker.upper().split("-", 1)[0]
    if head.startswith("KX"):
        head = head[2:]
    for prefix, _metric in _TICKER_METRIC_PREFIXES:
        if head.startswith(prefix):
            head = head[len(prefix):]
            break
    for code in sorted(TICKER_CITY_CODES, key=len, reverse=True):
        if head.startswith(code):
            city_name = TICKER_CITY_CODES[code]
            lat, lon = CITY_COORDS[city_name]
            return city_name, lat, lon
    return None


def _metric_from_ticker(ticker: str) -> str | None:
    head = ticker.upper().split("-", 1)[0]
    if head.startswith("KX"):
        head = head[2:]
    for prefix, metric in _TICKER_METRIC_PREFIXES:
        if head.startswith(prefix):
            return metric
    return None

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}

def _parse_band(title: str, ticker: str) -> tuple[float, float] | None:
    """Detect a temperature *band* contract → (low_edge, high_edge), else None.

    Kalshi band markets ask "be 85-86°" (a closed interval) rather than a
    one-sided "<NN"/">NN" threshold. The band has no comparator, so the
    single-threshold extraction in ``parse_title`` (which deliberately ignores
    bare integers to avoid grabbing the day-of-month) never fires. We read the
    two edges from the title's ``NN-NN°`` text, cross-checked against the
    ticker's ``B<midpoint>`` suffix (e.g. ``B85.5`` ⇒ ``[85, 86]``) as a fallback.
    """
    band_match = re.search(r"(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*°", title)
    if band_match:
        low_edge = float(band_match.group(1))
        high_edge = float(band_match.group(2))
        if high_edge >= low_edge:
            return low_edge, high_edge
    # Fallback: the ticker encodes the band midpoint in a "B<NN.5>" suffix.
    suffix_match = re.search(r"-B(\d+(?:\.\d+)?)\b", ticker.upper())
    if suffix_match:
        midpoint = float(suffix_match.group(1))
        low_edge = float(int(midpoint))  # floor → e.g. 85.5 → 85
        return low_edge, low_edge + 1.0
    return None


_UNCERTAINTY_KW = ["uncertain", "unsettled", "possible", "potential", "could", "may "]
_HIGH_CONFIDENCE_KW = ["high confidence", "confidence is high", "well-defined", "clear skies"]
_LOW_CONFIDENCE_KW = ["confidence is low", "low confidence"]


def parse_title(ticker: str, title: str) -> dict | None:
    """Parse Kalshi weather market title → structured question. Returns None on no match."""
    t = title.lower()

    # City — try the title first (longer names first to avoid partial matches),
    # then fall back to the city code embedded in the ticker (Kalshi's compact
    # weather titles, e.g. KXLOWTBOS, often omit the city from the title).
    city_name = lat = lon = None
    for name in sorted(CITY_COORDS, key=len, reverse=True):
        if name in t:
            city_name = name
            lat, lon = CITY_COORDS[name]
            break
    if lat is None:
        from_ticker = _city_from_ticker(ticker)
        if from_ticker is not None:
            city_name, lat, lon = from_ticker
    if lat is None:
        return None

    # Metric — title phrasing first (incl. "minimum/maximum temperature"), then
    # the ticker prefix (LOWT/HIGHT) as a fallback.
    if "high temp" in t or "high temperature" in t or "maximum temp" in t:
        metric = "temp_high"
    elif "low temp" in t or "low temperature" in t or "minimum temp" in t:
        metric = "temp_low"
    elif "rain" in t or "precipitation" in t or "precip" in t:
        metric = "precipitation"
    elif "wind" in t:
        metric = "wind"
    else:
        metric = _metric_from_ticker(ticker)
        if metric is None:
            return None

    # Band contracts ("be 85-86°") settle on a closed interval, not a one-sided
    # threshold. Detect these first: operator is "between" and the threshold is
    # the low edge, with the high edge carried in threshold_high.
    threshold_high = None
    band = _parse_band(title, ticker) if metric in ("temp_high", "temp_low") else None
    if band is not None:
        operator = "between"
        threshold, threshold_high = band
    else:
        # Operator — symbols (< / >) take precedence, then keyword phrasing.
        if "<" in title:
            operator = "below"
        elif ">" in title:
            operator = "above"
        elif any(kw in t for kw in ["above", "exceed", "or more", "at least", "over"]):
            operator = "above"
        elif any(kw in t for kw in ["below", "under", "less than"]):
            operator = "below"
        else:
            operator = "above"

        # Threshold — a number tied to a comparator (< / > or a keyword) or
        # written with °F. Never a bare integer (could be the day-of-month).
        threshold = None
        m = re.search(r"[<>]\s*(\d+(?:\.\d+)?)", t)
        if m:
            threshold = float(m.group(1))
        if threshold is None:
            m = re.search(r"(\d+(?:\.\d+)?)\s*°?\s*f\b", t)
            if m:
                threshold = float(m.group(1))
        if threshold is None:
            m = re.search(r"(?:above|below|under|over|exceed|at least|or more|less than)\s*(\d+(?:\.\d+)?)", t)
            if m:
                threshold = float(m.group(1))
        if threshold is None and metric == "wind":
            m = re.search(r"(\d+)\s*mph", t)
            if m:
                threshold = float(m.group(1))

        if threshold is None and metric not in ("precipitation",):
            return None

    # Date — month/day from the title, plus an explicit 4-digit year if present
    # (otherwise assume the current year).
    target_date = None
    m = re.search(
        r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
        r"[\s.]*(\d{1,2})",
        t,
    )
    if m:
        month = _MONTH_MAP.get(m.group(1)[:3])
        day = int(m.group(2))
        year_match = re.search(r"\b(20\d{2})\b", t)
        year = int(year_match.group(1)) if year_match else datetime.now().year
        if month:
            target_date = date(year, month, day)

    if target_date is None:
        return None

    parsed = {
        "city": city_name,
        "lat": lat,
        "lon": lon,
        "metric": metric,
        "threshold": threshold,
        "operator": operator,
        "target_date": target_date.isoformat(),
    }
    # Only band markets carry an upper edge; one-sided markets keep the original
    # single-threshold shape.
    if threshold_high is not None:
        parsed["threshold_high"] = threshold_high
    return parsed


def parse_discussion(text: str) -> dict:
    """Parse NWS AFD text → {confidence: str, key_points: list[str]}."""
    tl = text.lower()
    sentences = [s.strip() for s in re.split(r"[.!?\n]", text) if s.strip()]
    key_points = [s for s in sentences if any(kw in s.lower() for kw in _UNCERTAINTY_KW)][:5]

    uncertainty_count = sum(tl.count(kw) for kw in _UNCERTAINTY_KW)
    has_high_confidence_kw = any(kw in tl for kw in _HIGH_CONFIDENCE_KW)
    has_low_confidence_kw = any(kw in tl for kw in _LOW_CONFIDENCE_KW)

    if has_low_confidence_kw or uncertainty_count > 5:
        confidence = "low"
    elif has_high_confidence_kw and uncertainty_count == 0:
        confidence = "high"
    else:
        confidence = "medium"

    return {"confidence": confidence, "key_points": key_points}
