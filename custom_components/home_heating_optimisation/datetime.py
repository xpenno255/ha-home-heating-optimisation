"""Visitor mode end time."""

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities):
    if entry.runtime_data.dhw is not None:
        from .dhw.visitor import VisitorUntilDateTime

        async_add_entities([VisitorUntilDateTime(entry.runtime_data.dhw)])
