"""Everything that ticks in this process, assembled in one place.

Two kinds of loop live side by side here, and they are not the same thing:

* **infrastructure** — the vector sweep. No module, no trigger route, no
  `RunJournal` entry; it keeps its own journal and its own cross-replica lock,
  and takes nothing from the core but pacing.
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
from itop_ai_assistant.vector import register_vector_sweep


def build_background_tasks(deps: AppDeps, registry: TriggerRegistry) -> PeriodicTasks:
    """Assemble the process's periodic loops. Nothing runs until `start()`."""
    tasks = PeriodicTasks()
    register_vector_sweep(tasks, deps.vector)
    register_schedules(tasks, registry, deps)
    return tasks
