"""Everything that ticks in this process, assembled in one place.

Two kinds of loop live side by side here, and they are not the same thing:

* **infrastructure** — the vector sweep and the telemetry send. No module, no
  trigger route, no `RunJournal` entry; each keeps its own cross-replica
  exclusion and takes nothing from the core but pacing.
* **`schedule` triggers** — one loop per `ScheduleRoute` claimed by a business
  module, each tick wrapped in the same run frame a webhook or a synchronous
  request gets.

Adding either is one line, the same idiom as `build_registry` and
`vector.build`.
"""

from itop_ai_assistant.core.deps import AppDeps
from itop_ai_assistant.pipelines.registry import TriggerRegistry
from itop_ai_assistant.pipelines.scheduler import PeriodicTasks
from itop_ai_assistant.schedule.runner import register_schedules
from itop_ai_assistant.telemetry.sender import register_telemetry_send
from itop_ai_assistant.telemetry.telemetrydeck import TelemetryDeckSink
from itop_ai_assistant.vector import register_vector_sweep


def build_background_tasks(deps: AppDeps, registry: TriggerRegistry) -> PeriodicTasks:
    """Assemble the process's periodic loops. Nothing runs until `start()`."""
    tasks = PeriodicTasks()
    register_vector_sweep(
        tasks,
        deps.vector.config_store,
        deps.vector.vector_sources,
        deps.vector.vector_store,
        deps.vector.vector_sync,
        deps.vector.vector_journal,
        deps.counters,
    )
    register_telemetry_send(
        tasks,
        deps.config_store,
        deps.telemetry,
        deps.install,
        # Built here and not inside the loop, so that swapping the receiver
        # (REQ-009 R9) is a line in the composition root rather than an edit
        # to the loop that knows nothing about it. Constructing it opens
        # nothing: the HTTP client lives inside one `send`.
        TelemetryDeckSink(test_mode=deps.settings.telemetry_test_mode),
    )
    register_schedules(tasks, registry, deps)
    return tasks
