"""Download quality/count diagnostics without household identifiers or readings."""

from collections import Counter


async def async_get_config_entry_diagnostics(hass, entry):
    snapshot = entry.runtime_data.data
    return {
        "version": 2,
        "analytics": entry.runtime_data.analytics.quality()
        if entry.runtime_data.analytics
        else {"enabled": False},
        "operation": "observation_only",
        "room_count": len(snapshot.rooms),
        "input_quality_counts": dict(
            Counter(
                r.quality for room in snapshot.rooms for r in (room.air, room.target, room.demand)
            )
        ),
        "system_input_quality": {k: r.quality for k, r in snapshot.system.items()},
        "input_availability_percent": snapshot.input_availability,
    }
