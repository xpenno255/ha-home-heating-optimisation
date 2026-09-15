"""Read Recorder run boundaries without carrying observations across shutdowns."""

from datetime import timedelta

from homeassistant.util import dt as dt_util


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
    return [
        (dt_util.as_utc(a), dt_util.as_utc(b) if b else end + timedelta(microseconds=1), not bad)
        for a, b, bad in rows
    ]
