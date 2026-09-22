"""Journal schema, bounds and allowlists."""

SCHEMA_VERSION = 1
STORE_NAME = "journal"
MAX_EVENTS = 20000
RETENTION_SECONDS = 30 * 86400
SAVE_DELAY_SECONDS = 30
# A failed save is retried with doubling delays from SAVE_DELAY_SECONDS up to this
# cap, and retries stop after this many consecutive failures until the next record.
SAVE_BACKOFF_MAX_SECONDS = 8 * 60
MAX_SAVE_FAILURES = 10
QUERY_MAX_EVENTS = 2000
QUERY_MAX_HOURS = 720
QUERY_DEFAULT_HOURS = 24

KINDS = (
    "decision",
    "command_requested",
    "command_sent",
    "command_result",
    "readback",
    "manual_override",
    "schedule_change",
    "mode_change",
    "handover",
    "rollback",
    "adjustment_note",
    "gateway",
    "energy",
    "recommendation",
    "trial",
    "advisor_report",
    "advisor_followup",
    "migration",
    "dhw_schedule",
)
ORIGINS = ("controller", "user", "service", "source", "advisor", "unknown")
UNKNOWN = "unknown"
# Free text and raw evidence never leave private storage unless explicitly requested.
PRIVATE_KEYS = ("private_note", "note", "question", "evidence", "report_text", "raw")
