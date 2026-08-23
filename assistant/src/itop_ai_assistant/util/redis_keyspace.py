"""The whole Redis keyspace this service owns, in one place.

Purely declarative — no behavior, no Redis calls. Five owners keep their
adapters where they always lived (`settings/`, `state/`, `vector/`); this
module exists so a reader does not have to open all of them to know which
prefixes and TTLs are in play, and so a new key family has one obvious place
to register instead of inventing its own convention.

Key format and TTL *values* are unchanged by this module — it only names
constants that used to be declared locally in each adapter.
"""

# settings/config_store.py — runtime config overrides, no TTL (persist until reset)
CONFIG_PREFIX = "config:"

# settings/prompt_store.py — runtime prompt overrides, no TTL (persist until reset)
PROMPTS_PREFIX = "prompts:"

# state/ticket_state.py — per (ticket, module) AI state, and the one
# processing lock per ticket shared by whichever module claims it
TICKET_STATE_PREFIX = "ticket:"
TICKET_LOCK_PREFIX = "lock:"
# Safety timeout: the lock self-expires if processing dies without releasing it.
TICKET_LOCK_TTL_SECONDS = 300

# state/journal.py — processing-run journal, capped-recency index
RUN_PREFIX = "run:"
RUN_INDEX_KEY = "runs:index"
RUN_INDEX_MAX_ENTRIES = 1000
RUN_INDEX_SCAN_WINDOW = 500

# vector/state/sync_state.py — sweep cursors, reconcile clock, reindex flag, sweep lock.
# No TTL on cursors/flags: losing one is not an error but costs a full backfill
# (ADR-006 measures it in hours), so it is not treated like ticket/run state.
VECTOR_PREFIX = "vector:"
VECTOR_CURSOR_PREFIX = f"{VECTOR_PREFIX}cursor:"
VECTOR_FAMILY_SWEPT_PREFIX = f"{VECTOR_PREFIX}family-swept:"
VECTOR_RECONCILE_KEY = f"{VECTOR_PREFIX}reconcile"
VECTOR_REINDEX_KEY = f"{VECTOR_PREFIX}reindex"
VECTOR_SWEEP_LOCK_KEY = f"{VECTOR_PREFIX}sweep:lock"
# Short enough that a crashed replica does not block indexing for long,
# renewed often enough that an hours-long backfill keeps its lock.
VECTOR_SWEEP_LOCK_TTL_SECONDS = 120
VECTOR_SWEEP_LOCK_RENEW_INTERVAL_SECONDS = 40

# vector/state/index_journal.py — history of indexing runs, capped-recency index
VECTOR_RUN_PREFIX = f"{VECTOR_PREFIX}run:"
VECTOR_RUN_INDEX_KEY = f"{VECTOR_PREFIX}runs"
VECTOR_RUN_INDEX_MAX_ENTRIES = 50

# state/counters.py — one hash per UTC day, a field per counter (REQ-009 R3)
TELEMETRY_COUNTERS_PREFIX = "telemetry:counters:"
# Weeks, not a day: a day's counters must survive several missed sends in a row.
# Losing them to our own TTL is not the same thing as the missed day R8 allows.
TELEMETRY_COUNTERS_TTL_DAYS = 14


def days_to_seconds(days: int) -> int:
    """The one place `state_ttl_days`/`run_ttl_days` turn into Redis TTL seconds."""
    return days * 24 * 60 * 60
