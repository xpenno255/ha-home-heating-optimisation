"""Trial vocabulary and the closed allowlist of tunables a trial may change."""

SCHEMA = 1
STORE_SUFFIX = "trials"
MAX_TRIALS = 100
RETENTION_DAYS = 365
MAX_STORE_BYTES = 1000000
TICK_SECONDS = 60
MIN_DURATION_HOURS = 1
MAX_DURATION_HOURS = 168
MAX_TEXT = 500

# Only these tunables may ever be changed by a trial. Every write goes through the
# owning coordinator's set_tunable; no mode, enable switch, DHW setting or actuator
# service is reachable from here. max_step bounds the absolute change from baseline.
ALLOWED_PARAMETERS = {
    "room": {
        "trust_k": {"min": 0.0, "max": 1.0, "max_step": 0.2, "unit": None},
        "cap_up": {"min": 0.0, "max": 3.0, "max_step": 0.5, "unit": "K"},
        "cap_down": {"min": 0.0, "max": 3.0, "max_step": 0.5, "unit": "K"},
    },
    "boiler": {
        "design_flow": {"min": 30.0, "max": 80.0, "max_step": 5.0, "unit": "°C"},
        "design_outdoor": {"min": -15.0, "max": 10.0, "max_step": 2.0, "unit": "°C"},
        "return_ceiling": {"min": 30.0, "max": 70.0, "max_step": 5.0, "unit": "°C"},
    },
}
EXCLUDED_NOTE = (
    "dhw_delta, DHW protection, room or boiler modes, enable switches and every "
    "actuator service are excluded from trials."
)

STATES = (
    "proposed",
    "approved",
    "rejected",
    "running",
    "completed",
    "stopped",
    "rolled_back",
    "expired",
    "rollback_failed",
)
ACTIVE_STATES = ("running", "rollback_failed")
EVALUABLE_STATES = ("completed", "stopped", "rolled_back", "expired")
TRANSITIONS = {
    "proposed": {"approved", "rejected"},
    "approved": {"running", "rejected"},
    "running": {"completed", "stopped", "rolled_back", "expired", "rollback_failed"},
    "rollback_failed": {"rolled_back"},
    "rejected": set(),
    "completed": set(),
    "stopped": set(),
    "rolled_back": set(),
    "expired": set(),
}
OUTCOMES = ("improved", "no_change", "worse", "inconclusive")
PRIVATE_FIELDS = ("private_rationale", "private_note")
EVIDENCE_TYPE = "association"

# Conservative default stop allowances over the baseline analytics window totals.
DEFICIT_ALLOWANCE_KH = 1.0
OVERSHOOT_ALLOWANCE_KH = 1.0
MIN_COMFORT_FLOOR_C = 5.0
MAX_COMFORT_FLOOR_C = 25.0
METRIC_KEYS = ("deficit_degree_hours", "overshoot_degree_hours", "within_band", "coverage")
