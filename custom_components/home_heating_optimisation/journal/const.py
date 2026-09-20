"""Journal schema, bounds and allowlists."""

SCHEMA_VERSION = 1
STORE_NAME = "journal"
MAX_EVENTS = 20000
RETENTION_SECONDS = 30 * 86400
SAVE_DELAY_SECONDS = 30
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
)
ORIGINS = ("controller", "user", "service", "source", "advisor", "unknown")
UNKNOWN = "unknown"
# Free text and raw evidence never leave private storage unless explicitly requested.
PRIVATE_KEYS = ("private_note", "note", "question", "evidence", "report_text", "raw")
