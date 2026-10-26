"""Shared outdoor state kept by the comfort hub."""

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.home_heating_optimisation.control.comfort.hub import OTHubData

T0 = datetime(2026, 10, 26, 6, 0, tzinfo=timezone.utc)


def test_lagged_outdoor_seeds_from_running_mean_and_follows_slowly():
    hub = OTHubData(running_mean=7.0)
    assert hub.sample_outdoor_lagged(15.0, T0) == 7.0
    # Every room samples each cycle; repeats at the same moment do not move it.
    assert hub.sample_outdoor_lagged(15.0, T0) == 7.0
    after_8h = hub.sample_outdoor_lagged(15.0, T0 + timedelta(hours=8))
    assert after_8h == pytest.approx(7.0 + 8.0 * 0.632, abs=0.01)


def test_lagged_outdoor_without_running_mean_starts_at_live_value():
    hub = OTHubData()
    assert hub.sample_outdoor_lagged(11.0, T0) == 11.0
