"""Estimated boiler running efficiency: an opt-in, boiler-specific diagnostic.

This is a deliberately simple full-load-reference model, not a measured
efficiency. No water-flow or delivered-heat measurement exists; the estimate
only reflects an approximate gross-basis running efficiency implied by the
return temperature, under the manufacturer's full-load test conditions.

Model basis (British Gas 430/i, natural gas):
- Installation manual, p6: net input 30.9 kW; outputs 31.8 kW at 50/30 C and
  30.0 kW at 80/60 C (both a consistent 20 K flow/return differential). The
  40/30 C point is not used: its differential is different.
  https://www.freeboilermanuals.com/assets/pdf/British-Gas/BG-430i-Jun.pdf
- Gross-basis conversion: BRE SAP supporting document section 3.1 step 3 /
  Table 1 gives an explicitly indicative natural-gas factor of 0.901
  multiplying net efficiency.
  https://files.bregroup.com/bre-co-uk-file-library-copy/filelibrary/SAP/2016/CALCM-02---SAP-2016-SEASONAL-EFFICIENCY-VALUES-FOR-BOILERS--ALL-FUELS----DRAFT8.pdf

The interpolation between the two manufacturer points is a modelling
assumption, not a measured performance curve. No part-load correction is
applied: burner modulation only confirms firing, not a calibrated gas input.
Actual part-load efficiency can differ from this reference by several
percentage points; no numerical confidence interval has been established.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

MODEL_VERSION = "bg430i-return-linear-v1"
GROSS_CONVERSION_FACTOR = 0.901  # SAP CALCM-02, natural gas, indicative
NET_INPUT_KW = 30.9
REFERENCE_OUTPUT_50_30_KW = 31.8
REFERENCE_OUTPUT_80_60_KW = 30.0

RETURN_MIN_C = 30.0
RETURN_MAX_C = 60.0

PROFILE_DISABLED = "disabled"
PROFILE_BG430I_NATURAL_GAS = "bg430i_natural_gas"
PROFILES = (PROFILE_DISABLED, PROFILE_BG430I_NATURAL_GAS)
CONF_EFFICIENCY_PROFILE = "efficiency_profile"

STARTUP_MINUTES = 5.0

# Status strings, each a short honest reason the estimate is/is not available.
STATUS_DISABLED = "disabled"
STATUS_OFF = "off"
STATUS_UNSUPPORTED_PROFILE = "unsupported_profile"
STATUS_BURNER_UNAVAILABLE = "burner_unavailable"
STATUS_AWAITING_IGNITION = "awaiting_observed_ignition"
STATUS_STARTUP = "startup_exclusion"
STATUS_RETURN_UNAVAILABLE = "return_unavailable"
STATUS_RETURN_OUT_OF_RANGE = "return_out_of_range"
STATUS_OK = "ok"


@dataclass(frozen=True)
class EfficiencyEstimate:
    percent: int | None
    status: str


def _bg430i_reference_percent(return_c: float) -> float | None:
    """Gross-basis full-load-reference efficiency at `return_c` (30-60 C only).

    `t = (return_c - 30) / 30` maps the 20 K flow/return differential's return
    leg linearly between the two manufacturer test points; `reference_output_kw`
    interpolates output between them. 92.724% at 30 C, 90.1% at 45 C (midpoint),
    87.476% at 60 C.
    """
    if not RETURN_MIN_C <= return_c <= RETURN_MAX_C:
        return None
    t = (return_c - 30.0) / 30.0
    reference_output_kw = (
        REFERENCE_OUTPUT_50_30_KW - (REFERENCE_OUTPUT_50_30_KW - REFERENCE_OUTPUT_80_60_KW) * t
    )
    return 100.0 * GROSS_CONVERSION_FACTOR * reference_output_kw / NET_INPUT_KW


def estimate(
    profile: str,
    *,
    heating_active: bool | None,
    burner_power: float | None,
    return_c: float | None,
    burn_seconds: float | None,
) -> EfficiencyEstimate:
    """Compute the estimate for one cycle. Pure; no HA or clock access.

    `heating_active` is the current heating-active binary sensor state
    (`None` when unknown/unavailable). `burner_power` and `return_c` are
    already freshness-checked, finite values in their valid ranges (or
    `None`). `burn_seconds` is the elapsed time since the observed off->on
    ignition that started the current burn, or `None` while unknown (no
    ignition observed yet, e.g. immediately after a restart mid-burn).
    """
    if profile == PROFILE_DISABLED or not profile:
        return EfficiencyEstimate(None, STATUS_DISABLED)
    if profile not in PROFILES:
        return EfficiencyEstimate(None, STATUS_UNSUPPORTED_PROFILE)
    if heating_active is None:
        return EfficiencyEstimate(None, STATUS_BURNER_UNAVAILABLE)
    if heating_active is False:
        return EfficiencyEstimate(None, STATUS_OFF)
    if burner_power is None or not isfinite(burner_power) or not 0 <= burner_power <= 100:
        return EfficiencyEstimate(None, STATUS_BURNER_UNAVAILABLE)
    if burner_power == 0:
        return EfficiencyEstimate(None, STATUS_OFF)
    if burn_seconds is None or not isfinite(burn_seconds) or burn_seconds < 0:
        return EfficiencyEstimate(None, STATUS_AWAITING_IGNITION)
    if burn_seconds < STARTUP_MINUTES * 60.0:
        return EfficiencyEstimate(None, STATUS_STARTUP)
    if return_c is None or not isfinite(return_c):
        return EfficiencyEstimate(None, STATUS_RETURN_UNAVAILABLE)
    percent = _bg430i_reference_percent(return_c)
    if percent is None:
        return EfficiencyEstimate(None, STATUS_RETURN_OUT_OF_RANGE)
    return EfficiencyEstimate(round(percent), STATUS_OK)
