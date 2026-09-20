"""Bounds and definitions for metered-energy evidence."""

SCHEMA_VERSION = 1
BUCKET_SECONDS = 300
MAX_DAYS = 90
MAX_BUCKETS = MAX_DAYS * 86400 // BUCKET_SECONDS
MAX_METERS = 3
SAVE_EVERY_BUCKETS = 3
KINDS = ("fuel_input", "delivered_heat", "electricity")
UNITS = ("kWh", "Wh", "MWh", "m³")
MJ_PER_KWH = 3.6
DEFAULT_CALORIFIC_MJ_M3 = 39.5
DEFAULT_VOLUME_CORRECTION = 1.02264
DEGREE_HOUR_BASE_C = 15.5
MIN_COVERAGE = 80
MIN_CONTEXT_SHARE = 0.5
DHW_SHARE_TOLERANCE_POINTS = 15
ALLOCATION_UNKNOWN_SOFT_LIMIT = 0.3
MIN_MEDIUM_CONFIDENCE_DAYS = 3
GAP_FACTOR = 1.5
QUALITIES = ("ok", "reset", "rollover", "gap", "unit_unknown", "missing")
ALLOCATIONS = ("heating", "dhw", "idle", "unknown")
INTERVENTION_KINDS = frozenset(
    {"command_sent", "mode_change", "manual_override", "adjustment_note", "trial"}
)
ASSOCIATION_NOTE = "association, not causal evidence"
NO_SAVINGS_LIMITATION = "No savings conclusion: metering/context insufficient"
