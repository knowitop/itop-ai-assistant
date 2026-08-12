"""The ports contract: what the container must satisfy, and what it must not offer.

The real guarantee behind these ports is static, so the first test here is
mostly a mypy assertion that happens to also run: if `AppDeps` ever stops
satisfying `RunDeps` — a field renamed, a type narrowed the wrong way — the
strict mypy gate fails on this file, at the seam, instead of somewhere down in
a module that happens to use the missing member.

The second test pins the deliberate *absences*. Those cannot be caught by
`isinstance` (a protocol is satisfied by having more, never by having less), so
they are asserted against the protocol's own member list.
"""

import unittest

from itop_ai_assistant.config import get_settings
from itop_ai_assistant.deps import AppDeps, build_deps
from itop_ai_assistant.pipelines.ports import (
    ItopAccess,
    LockPort,
    RunDeps,
    RunFrameJournal,
    StepJournal,
    TicketStatePort,
)


def _requires_run_deps(deps: RunDeps) -> RunDeps:
    """Typed narrowly on purpose: passing an `AppDeps` here is the assertion."""
    return deps


class TestContainerSatisfiesTheContract(unittest.TestCase):
    def test_app_deps_is_accepted_where_a_handler_expects_run_deps(self):
        deps: AppDeps = build_deps(get_settings())

        self.assertIs(_requires_run_deps(deps), deps)

    def test_the_narrow_ports_are_served_by_the_container_too(self):
        """What `TicketRun.handle` takes apart — pinned member by member."""
        deps = build_deps(get_settings())

        lock: LockPort = deps.state_manager
        state: TicketStatePort = deps.state_manager
        itop: ItopAccess = deps.itop
        step: StepJournal = deps.journal
        frame: RunFrameJournal = deps.journal

        self.assertIs(lock, state)
        self.assertIs(step, frame)
        self.assertIs(itop, deps.itop)


class TestPortsWithholdWhatRunsMustNotReach(unittest.TestCase):
    """Ownership stays at the composition root — see `pipelines/ports.py`."""

    def test_no_port_hands_out_the_shutdown_of_shared_resources(self):
        for port in (RunDeps, LockPort, TicketStatePort, ItopAccess, StepJournal, RunFrameJournal):
            with self.subTest(port=port.__name__):
                self.assertNotIn("aclose", port.__protocol_attrs__)

    def test_a_handler_cannot_read_startup_settings_behind_the_config_store(self):
        """Runtime overrides are what make an admin edit apply without a restart;
        a handler reaching `settings` directly would silently bypass them."""
        self.assertNotIn("settings", RunDeps.__protocol_attrs__)

    def test_the_agent_loop_half_of_the_journal_cannot_open_or_close_a_run(self):
        """A run is opened exactly once, by the entry point (`journalled_run`)."""
        self.assertNotIn("start", StepJournal.__protocol_attrs__)
        self.assertNotIn("finish", StepJournal.__protocol_attrs__)
        # The frame half, by contrast, is exactly the one that may
        self.assertIn("start", RunFrameJournal.__protocol_attrs__)
        self.assertIn("finish", RunFrameJournal.__protocol_attrs__)
