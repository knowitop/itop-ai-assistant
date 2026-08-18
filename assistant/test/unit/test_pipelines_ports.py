"""The ports contract: what the container must satisfy, and what it must not offer.

The real guarantee behind these ports is static, so the first test here is
mostly a mypy assertion that happens to also run: if `AppDeps` ever stops
satisfying `RunDeps` — a field renamed, a type narrowed the wrong way — the
strict mypy gate fails on this file, at the seam, instead of somewhere down in
a module that happens to use the missing member.

The second test pins the deliberate *absences*. Those cannot be caught by
`isinstance` (a protocol is satisfied by having more, never by having less), so
they are asserted against the protocol's own member list.

Every test here is annotated `-> None` for a reason that is not style: mypy
does not check the body of an unannotated function, so without the return type
the assertion in the first test is invisible to the very gate that is supposed
to make it (found in TASK-029, where the same test shape was written for
`IndexerDeps` — until the annotation went on, breaking a port on purpose was
caught at the call sites and not here).
"""

import unittest
from typing import get_protocol_members

from itop_ai_assistant.config import get_settings
from itop_ai_assistant.core.deps import AppDeps, build_deps
from itop_ai_assistant.itop.connection import AiIdentity
from itop_ai_assistant.pipelines.ports import (
    LockPort,
    RunDeps,
    RunFrameJournal,
    StepJournal,
    TicketStatePort,
)
from itop_ai_assistant.pipelines.registry import build_registry
from itop_ai_assistant.repositories.sets import ItopRepositories


def _requires_run_deps(deps: RunDeps) -> RunDeps:
    """Typed narrowly on purpose: passing an `AppDeps` here is the assertion."""
    return deps


class TestContainerSatisfiesTheContract(unittest.TestCase):
    def test_app_deps_is_accepted_where_a_handler_expects_run_deps(self) -> None:
        settings = get_settings()
        deps: AppDeps = build_deps(settings, build_registry(settings))

        self.assertIs(_requires_run_deps(deps), deps)

    def test_the_narrow_ports_are_served_by_the_container_too(self) -> None:
        """What `TicketRun.handle` takes apart — pinned member by member."""
        settings = get_settings()
        deps = build_deps(settings, build_registry(settings))

        lock: LockPort = deps.state_manager
        state: TicketStatePort = deps.state_manager
        itop: ItopRepositories = deps.itop
        identity: AiIdentity = deps.ai_identity
        step: StepJournal = deps.journal
        frame: RunFrameJournal = deps.journal

        self.assertIs(lock, state)
        self.assertIs(step, frame)
        self.assertIs(itop, deps.itop)
        self.assertIs(identity, deps.itop_connection)


class TestPortsWithholdWhatRunsMustNotReach(unittest.TestCase):
    """Ownership stays at the composition root — see `pipelines/ports.py`."""

    def test_no_port_hands_out_the_shutdown_of_shared_resources(self) -> None:
        for port in (RunDeps, LockPort, TicketStatePort, AiIdentity, StepJournal, RunFrameJournal):
            with self.subTest(port=port.__name__):
                self.assertNotIn("aclose", get_protocol_members(port))

    def test_a_handler_cannot_read_startup_settings_behind_the_config_store(self) -> None:
        """Runtime overrides are what make an admin edit apply without a restart;
        a handler reaching `settings` directly would silently bypass them."""
        self.assertNotIn("settings", get_protocol_members(RunDeps))

    def test_the_agent_loop_half_of_the_journal_cannot_open_or_close_a_run(self) -> None:
        """A run is opened exactly once, by the entry point (`journalled_run`)."""
        self.assertNotIn("start", get_protocol_members(StepJournal))
        self.assertNotIn("finish", get_protocol_members(StepJournal))
        # The frame half, by contrast, is exactly the one that may
        self.assertIn("start", get_protocol_members(RunFrameJournal))
        self.assertIn("finish", get_protocol_members(RunFrameJournal))
