from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

# India has no DST, so a fixed offset avoids depending on a tz database
# (not guaranteed on every serverless runtime).
IST = timezone(timedelta(hours=5, minutes=30), "IST")


def utcnow() -> datetime:
    return datetime.now(UTC)


def fmt_ist(dt: datetime) -> str:
    return dt.astimezone(IST).strftime("%d %b %Y, %H:%M IST")
