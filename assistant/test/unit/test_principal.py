"""The identity of a run, and the one rule about it: the token stays inside."""

import logging
import unittest
from uuid import UUID

from itop_ai_assistant.core.principal import Principal
from itop_ai_assistant.pipelines.context import RunContext

_TOKEN = "s3cret-engineer-token"
_PID = UUID("3f2a1c4e-0000-0000-0000-000000000042")


def _delegated() -> Principal:
    return Principal.delegated(_TOKEN, login="jdoe", name="John Doe")


class TestTheTokenNeverLeaks(unittest.TestCase):
    def test_not_in_any_rendering_of_the_principal(self):
        principal = _delegated()

        renderings = [repr(principal), str(principal), f"{principal}", f"{principal!r}", principal.label]

        for rendering in renderings:
            with self.subTest(rendering=rendering):
                self.assertNotIn(_TOKEN, rendering)

    def test_not_in_the_credentials_it_carries(self):
        self.assertNotIn(_TOKEN, repr(_delegated().auth))
        # …while the credentials themselves are of course still usable
        self.assertEqual(_delegated().auth.token, _TOKEN)

    def test_not_in_a_log_record(self):
        principal = _delegated()
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "run as %s", (principal,), None)

        self.assertNotIn(_TOKEN, logging.Formatter().format(record))

    def test_not_in_the_comment_written_to_itop(self):
        run = RunContext(processing_id=_PID, module="console", principal=_delegated())

        self.assertNotIn(_TOKEN, run.comment)


class TestPrincipals(unittest.TestCase):
    def test_the_service_account_carries_no_credentials_of_its_own(self):
        # It uses the connection's, which is what keeps step A free of new secrets
        self.assertIsNone(Principal.service().auth)
        self.assertEqual(Principal.service().label, "service")

    def test_a_delegated_principal_names_the_person(self):
        principal = _delegated()

        self.assertEqual(principal.kind, "delegated")
        self.assertEqual(principal.label, "engineer:jdoe")
        self.assertEqual(principal.on_behalf_of, "John Doe")


class TestTheComment(unittest.TestCase):
    def test_names_the_module_and_the_run(self):
        run = RunContext(processing_id=_PID, module="intake")

        self.assertEqual(run.comment, f"AI assistant · intake · run {_PID}")

    def test_a_service_run_is_not_on_behalf_of_anyone(self):
        self.assertNotIn("on behalf of", RunContext(processing_id=_PID, module="intake").comment)

    def test_a_delegated_run_says_who_it_acts_for(self):
        run = RunContext(processing_id=_PID, module="console", principal=_delegated())

        self.assertTrue(run.comment.endswith("· on behalf of John Doe"))

    def test_a_run_acts_as_the_service_account_unless_told_otherwise(self):
        self.assertEqual(RunContext(processing_id=_PID, module="intake").principal, Principal.service())


if __name__ == "__main__":
    unittest.main()
