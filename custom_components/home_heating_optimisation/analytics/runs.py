"""Read Recorder run boundaries without carrying observations across shutdowns."""

from datetime import UTC, timedelta


def recorded_runs(hass, start, end):
    from homeassistant.components.recorder.db_schema import RecorderRuns
    from homeassistant.components.recorder.util import session_scope
    from sqlalchemy import or_, select

    with session_scope(hass=hass, read_only=True) as session:
        rows = session.execute(
            select(RecorderRuns.start, RecorderRuns.end, RecorderRuns.closed_incorrect).where(
                RecorderRuns.start <= end,
                or_(RecorderRuns.end >= start, RecorderRuns.end.is_(None)),
            )
        ).all()

    # Recorder's SQLite DateTime columns are naive UTC. as_utc() interprets
    # naive datetimes in HA's local timezone, shifting run/gap boundaries.
    def utc(value):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    return [
        (utc(a), utc(b) if b else end + timedelta(microseconds=1), not bad) for a, b, bad in rows
    ]
