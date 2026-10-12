"""Hourly outdoor forecast cache for the optimum predictor (pure; the fetch is in the runtime).

The forecast is optional. When it is missing, stale or the entity is unset the current
outdoor reading is used for the whole prediction, and the prediction says so.
"""

import math
from datetime import datetime, timedelta, timezone

REFRESH = timedelta(minutes=30)
STALE = timedelta(hours=3)


def parse_forecast(response, entity_id, fahrenheit=False):
    """Sorted ``[(aware datetime, °C)]`` from a ``weather.get_forecasts`` response."""
    entries = ((response or {}).get(entity_id) or {}).get("forecast")
    points = {}
    for item in entries if isinstance(entries, list) else []:
        try:
            when = datetime.fromisoformat(str(item["datetime"]))
            value = float(item["temperature"])
        except KeyError, TypeError, ValueError:
            continue
        if not math.isfinite(value):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        points[when] = (value - 32) * 5 / 9 if fahrenheit else value
    return sorted(points.items())


class Forecast:
    def __init__(self):
        self.points = []
        self.fetched_at = None

    def update(self, points, fetched_at):
        self.points, self.fetched_at = list(points), fetched_at

    def due(self, now):
        return self.fetched_at is None or now - self.fetched_at >= REFRESH

    def usable(self, now):
        return (
            len(self.points) > 0
            and self.fetched_at is not None
            and timedelta(0) <= now - self.fetched_at <= STALE
            and self.points[-1][0] >= now
        )

    def outdoor_function(self, now, current):
        """``(outdoor_at, source)``: ``outdoor_at(datetime) -> °C``, source forecast|current."""
        if not self.usable(now):
            return (lambda _when: current), "current"
        points = self.points

        def outdoor_at(when):
            if when <= points[0][0]:
                return points[0][1]
            for (t1, v1), (t2, v2) in zip(points, points[1:]):
                if when <= t2:
                    return v1 + (v2 - v1) * (when - t1) / (t2 - t1)
            return points[-1][1]

        return outdoor_at, "forecast"
