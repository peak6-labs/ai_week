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
}

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}

_UNCERTAINTY_KW = ["uncertain", "unsettled", "possible", "potential", "could", "may "]
_HIGH_CONFIDENCE_KW = ["high confidence", "confidence is high", "well-defined", "clear skies"]
_LOW_CONFIDENCE_KW = ["confidence is low", "low confidence"]


def parse_title(ticker: str, title: str) -> dict | None:
    """Parse Kalshi weather market title → structured question. Returns None on no match."""
    t = title.lower()

    # City — try longer names first to avoid partial matches
    city_name = lat = lon = None
    for name in sorted(CITY_COORDS, key=len, reverse=True):
        if name in t:
            city_name = name
            lat, lon = CITY_COORDS[name]
            break
    if lat is None:
        return None

    # Metric
    if "high temp" in t or "high temperature" in t:
        metric = "temp_high"
    elif "low temp" in t or "low temperature" in t:
        metric = "temp_low"
    elif "rain" in t or "precipitation" in t or "precip" in t:
        metric = "precipitation"
    elif "wind" in t:
        metric = "wind"
    else:
        return None

    # Operator
    if any(kw in t for kw in ["above", "exceed", "or more", "at least", "over"]):
        operator = "above"
    elif any(kw in t for kw in ["below", "under", "less than"]):
        operator = "below"
    else:
        operator = "above"

    # Threshold (temperature °F or wind mph)
    threshold = None
    m = re.search(r"(\d+)\s*°?\s*f\b", t)
    if m:
        threshold = float(m.group(1))
    elif metric == "wind":
        m = re.search(r"(\d+)\s*mph", t)
        if m:
            threshold = float(m.group(1))

    if threshold is None and metric not in ("precipitation",):
        return None

    # Date
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
        year = datetime.utcnow().year
        if month:
            target_date = date(year, month, day)

    if target_date is None:
        return None

    return {
        "city": city_name,
        "lat": lat,
        "lon": lon,
        "metric": metric,
        "threshold": threshold,
        "operator": operator,
        "target_date": target_date.isoformat(),
    }


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
