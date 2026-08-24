"""The whole Redis keyspace this service owns, in one place.

Purely declarative — no behavior, no Redis calls. Six owners keep their
adapters where they always lived (`settings/`, `state/`, `vector/`,
`telemetry/`); this module exists so a reader does not have to open all of them
to know which prefixes and TTLs are in play, and so a new key family has one
obvious place to register instead of inventing its own convention.

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

# telemetry/install.py — what we remember about this installation between
# restarts: the anonymous id it generated for itself and the last admin-UI
# language it was seen in (REQ-009 R1, R10). One hash, two fields, and no TTL
# on purpose — expiring the id would make one installation look like a new one
# every time it lapsed, which is the single number the whole requirement is
# built to answer. A Redis reset does exactly that, and `docs/telemetry.md`
# says so rather than the code working around it.
TELEMETRY_INSTALL_KEY = "telemetry:install"
TELEMETRY_INSTALL_ID_FIELD = "id"
TELEMETRY_INSTALL_LANGUAGE_FIELD = "language"
# When this installation was first seen, and the day its setup wizard was
# finished — the two dates the sender compares against before it may send
# anything at all (REQ-009 R6).
TELEMETRY_INSTALL_FIRST_SEEN_FIELD = "first_seen"
TELEMETRY_INSTALL_SETUP_DAY_FIELD = "setup_day"

# telemetry/install.py — one key per UTC day, claimed before a send is
# attempted. It means "this day is taken", not "this day was delivered": a
# replica that claims and then fails loses the day, which R8 allows, while
# double counting is what would corrupt the one number the requirement asks
# for. Claim-and-forget rather than a renewed lock like the vector sweep's —
# a send takes seconds, and the TTL is what releases it.
TELEMETRY_SENT_DAY_PREFIX = "telemetry:sent:"
TELEMETRY_SENT_DAY_TTL_DAYS = 3

# state/counters.py — one hash per UTC day, a field per counter (REQ-009 R3)
TELEMETRY_COUNTERS_PREFIX = "telemetry:counters:"
# Long enough to be read, and no longer. A day is asked for exactly once, by
# the tick that sends it the day after (`telemetry/sender.py`), and the sender
# never looks further back than yesterday — R8 spends a missed day rather than
# queueing it. So the reachable window is one day plus the tick that reads it,
# and the third day is the slack. Deliberately the same number as the claim
# above: the key that says a day is taken and the counters it was taken for
# fall out of Redis together.
TELEMETRY_COUNTERS_TTL_DAYS = 3


def days_to_seconds(days: int) -> int:
    """The one place `state_ttl_days`/`run_ttl_days` turn into Redis TTL seconds."""
    return days * 24 * 60 * 60
