"""Serve the HHO rooms card (frontend/hho-rooms-card.js) to the Home Assistant frontend."""

import logging
from pathlib import Path

from .const import DOMAIN, VERSION

LOGGER = logging.getLogger(__name__)
URL = f"/{DOMAIN}/hho-rooms-card.js"
_REGISTERED = f"{DOMAIN}_card_registered"


async def async_register_card(hass) -> None:
    """Serve the card and load it on every dashboard; once per Home Assistant run.

    Skipped without the http or frontend components (tests, headless setups).
    """
    if hass.data.get(_REGISTERED):
        return
    if getattr(hass, "http", None) is None or "frontend" not in hass.config.components:
        return
    try:
        from homeassistant.components.frontend import add_extra_js_url
        from homeassistant.components.http import StaticPathConfig

        path = Path(__file__).parent / "frontend" / "hho-rooms-card.js"
        await hass.http.async_register_static_paths([StaticPathConfig(URL, str(path), False)])
        # The version in the URL makes browsers fetch the new card after an update.
        add_extra_js_url(hass, f"{URL}?v={VERSION}")
    except Exception:  # noqa: BLE001 - the card is a convenience; heating must still start
        LOGGER.exception("HHO rooms card could not be registered")
        return
    hass.data[_REGISTERED] = True
