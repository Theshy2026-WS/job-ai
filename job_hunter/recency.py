"""Publication-date filtering in the applicant's New York calendar.

Only an actual publication field can establish recency. Job creation, last
update, and local discovery timestamps are intentionally not fallback dates.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timezone
import math
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _new_york():
    try:
        return ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(
            "America/New_York timezone data is missing. Install the tzdata "
            "dependency before running publication-date filtering."
        ) from exc


def subtract_calendar_months(value: date, months: int) -> date:
    """Subtract months, clamping the day when the target month is shorter."""
    if isinstance(months, bool) or not isinstance(months, int) or months < 1:
        raise ValueError("max_posting_age_months must be a positive integer")
    month_index = value.year * 12 + value.month - 1 - months
    year, month_zero = divmod(month_index, 12)
    if year < 1:
        raise ValueError("max_posting_age_months exceeds the supported calendar")
    month = month_zero + 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def _publication_value(value: Any) -> tuple[date | datetime, str] | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value, value.isoformat()
    if isinstance(value, date):
        return value, value.isoformat()
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            return None
        try:
            seconds = value / 1000 if abs(value) >= 100_000_000_000 else value
            parsed = datetime.fromtimestamp(seconds, tz=timezone.utc)
            return parsed, parsed.isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            parsed = date.fromisoformat(raw)
            return parsed, parsed.isoformat()
        except ValueError:
            return None
    # Numeric strings are accepted only at conventional modern epoch widths.
    # A bare calendar year or YYYYMMDD must not silently become an epoch date.
    if re.fullmatch(r"[-+]?\d{10,13}(?:\.\d+)?", raw):
        return _publication_value(float(raw))
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed, parsed.isoformat()


def assess_posting(job: dict, profile: dict, now: date | datetime | None = None) -> dict:
    """Return whether a reliable publication date is inside the rolling window.

    Calendar-month boundaries are inclusive. A timestamp after ``now`` is held
    as future; when ``now`` is a date rather than an instant, only its calendar
    day is compared. Date-only publication values represent advertised calendar
    dates and are not assigned an invented time of day.
    """
    months = profile.get("preferences", {}).get("max_posting_age_months", 2)
    zone = _new_york()
    if now is None:
        current = datetime.now(timezone.utc).astimezone(zone)
        today, current_instant = current.date(), current
    elif isinstance(now, datetime):
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware or a calendar date")
        current = now.astimezone(zone)
        today, current_instant = current.date(), current
    elif isinstance(now, date):
        today, current_instant = now, None
    else:
        raise ValueError("now must be a timezone-aware datetime or a date")
    cutoff = subtract_calendar_months(today, months)
    result = {
        "posting_recency": "unknown",
        "posted_at": None,
        "cutoff_date": cutoff.isoformat(),
        "posting_age_days": None,
        "eligible": False,
        "reason": "Publication date is not confirmed; creation, update and discovery dates are not used.",
        "publication_date_source": None,
    }
    parsed = None
    for field in ("published_at", "date_posted"):
        candidate = _publication_value(job.get(field))
        if candidate is not None:
            parsed, normalized = candidate
            result["posted_at"] = normalized
            result["publication_date_source"] = job.get(field + "_source") or field
            break
    if parsed is None:
        return result
    published_date = parsed.astimezone(zone).date() if isinstance(parsed, datetime) else parsed
    result["posting_age_days"] = (today - published_date).days
    is_future = published_date > today or (
        isinstance(parsed, datetime) and current_instant is not None
        and parsed.astimezone(timezone.utc) > current_instant.astimezone(timezone.utc)
    )
    if is_future:
        result.update(posting_recency="future", reason="Publication date is in the future; held for review.")
    elif published_date < cutoff:
        result.update(posting_recency="old", reason=f"Posting predates the {months}-calendar-month cutoff {cutoff.isoformat()}.")
    else:
        result.update(posting_recency="recent", eligible=True,
                      reason=f"Publication date is within {months} calendar months, from {cutoff.isoformat()} through {today.isoformat()}.")
    return result
