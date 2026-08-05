import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs

import httpx

from itop_ai_assistant.itop_client import Itop, ItopAuth


class _CapturingTransport(httpx.AsyncBaseTransport):
    """Captures the payload and the credentials of each request.

    Credentials travel two ways — user and password as form fields, a token as
    the Auth-Token header — so both are recorded to pin which identity a request
    actually went out as.
    """

    def __init__(self, objects: dict | None = None):
        self.requests: list[dict] = []
        self.forms: list[dict] = []
        self.tokens: list[str | None] = []
        self._objects = objects

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = parse_qs(request.content.decode())
        self.requests.append(json.loads(body["json_data"][0]))
        self.forms.append({key: values[0] for key, values in body.items() if key != "json_data"})
        self.tokens.append(request.headers.get("Auth-Token"))
        return httpx.Response(200, json={"code": 0, "objects": self._objects})


def _make_itop(objects: dict | None = None) -> tuple[Itop, _CapturingTransport]:
    transport = _CapturingTransport(objects)
    itop = Itop(url="http://mock/rest.php", version="1.3", auth_user="u", auth_pwd="p", transport=transport)
    return itop, transport


_SERVICE_OBJECTS = {
    "Service::5": {
        "key": "5",
        "fields": {"name": "IT", "description": "desc", "status": "production"},
    }
}


class TestSchemaFindOutputFields(unittest.IsolatedAsyncioTestCase):
    async def test_projection_sent_as_output_fields(self):
        itop, transport = _make_itop(_SERVICE_OBJECTS)

        await itop.schema("Service").find({"id": 5}, projection=["id", "name", "description"])

        self.assertEqual(transport.requests[0]["output_fields"], "id,name,description")

    async def test_no_projection_requests_everything(self):
        itop, transport = _make_itop(_SERVICE_OBJECTS)

        await itop.schema("Service").find({"id": 5})

        self.assertEqual(transport.requests[0]["output_fields"], "*+")

    async def test_result_filtered_to_projection(self):
        itop, _ = _make_itop(_SERVICE_OBJECTS)

        result = await itop.schema("Service").find({"id": 5}, projection=["id", "name"])

        self.assertEqual(result, [{"id": "5", "name": "IT"}])

    async def test_result_without_projection_includes_all_fields(self):
        itop, _ = _make_itop(_SERVICE_OBJECTS)

        result = await itop.schema("Service").find({"id": 5})

        self.assertEqual(result[0]["status"], "production")
        self.assertEqual(result[0]["id"], "5")

    async def test_no_objects_returns_empty_list(self):
        itop, _ = _make_itop(objects=None)

        result = await itop.schema("Service").find({"id": 5}, projection=["id", "name"])

        self.assertEqual(result, [])


class TestComment(unittest.IsolatedAsyncioTestCase):
    """The comment iTop records in the object's change history."""

    async def test_the_clients_comment_reaches_every_operation(self):
        cases = [
            ("find", lambda s: s.find({"id": 5})),
            ("insert", lambda s: s.insert({"name": "IT"})),
            ("update", lambda s: s.update({"id": 5}, {"name": "IT"})),
            ("remove", lambda s: s.remove({"id": 5})),
            ("apply_stimulus", lambda s: s.apply_stimulus({"id": 5}, {}, stimulus="ev_assign")),
        ]
        for name, call in cases:
            with self.subTest(operation=name):
                itop, transport = _make_itop(_SERVICE_OBJECTS)

                await call(itop.as_(comment="AI assistant · intake · run 42").schema("Service"))

                self.assertEqual(transport.requests[0]["comment"], "AI assistant · intake · run 42")

    async def test_without_a_comment_the_library_keeps_its_own_default(self):
        itop, transport = _make_itop(_SERVICE_OBJECTS)

        await itop.schema("Service").update({"id": 5}, {"name": "IT"})

        self.assertEqual(transport.requests[0]["comment"], "Update Service")


class TestPrincipalViews(unittest.IsolatedAsyncioTestCase):
    """`as_()`: the same connection seen through different credentials."""

    async def test_the_views_credentials_reach_the_request(self):
        itop, transport = _make_itop(_SERVICE_OBJECTS)

        await itop.as_(auth=ItopAuth(token="engineer-token")).schema("Service").find({"id": 5})

        self.assertEqual(transport.tokens[0], "engineer-token")
        self.assertNotIn("auth_user", transport.forms[0])

    async def test_a_view_does_not_touch_the_client_it_came_from(self):
        itop, transport = _make_itop(_SERVICE_OBJECTS)
        view = itop.as_(auth=ItopAuth(token="engineer-token"), comment="run 42")

        await view.schema("Service").find({"id": 5})
        await itop.schema("Service").find({"id": 5})

        self.assertEqual(transport.forms[1]["auth_user"], "u")
        self.assertIsNone(transport.tokens[1])
        self.assertEqual(transport.requests[1]["comment"], "Get Service")

    async def test_nothing_to_override_gives_back_the_same_client(self):
        itop, _ = _make_itop()

        self.assertIs(itop.as_(), itop)

    async def test_closing_a_view_leaves_the_shared_pool_open(self):
        itop, _ = _make_itop(_SERVICE_OBJECTS)
        view = itop.as_(auth=ItopAuth(token="engineer-token"))

        await view.aclose()

        self.assertFalse(itop._http.is_closed)
        # and the pool still serves both
        await view.schema("Service").find({"id": 5})
        await itop.schema("Service").find({"id": 5})

    async def test_closing_the_owner_closes_the_pool(self):
        itop, _ = _make_itop()

        await itop.aclose()

        self.assertTrue(itop._http.is_closed)


_DATAMODEL_XML = """<?xml version="1.0" encoding="UTF-8"?>
<itop_design>
 <classes>
  <class id="UserRequest">
   <fields>
    <field id="caller_name" type="AttributeExternalField">
     <extkey_attcode>caller_id</extkey_attcode>
     <target_attcode>friendlyname</target_attcode>
    </field>
    <field id="caller_id" type="AttributeExternalKey">
     <target_class>Person</target_class>
    </field>
   </fields>
  </class>
  <class id="Person"><fields/></class>
 </classes>
</itop_design>
"""


class TestViewsWithADataModel(unittest.IsolatedAsyncioTestCase):
    """A datamodel gives Schema its own reasons to call iTop — those must inherit
    the view too, or a write on behalf of an engineer would resolve external keys
    as the service account and silently return objects they cannot see."""

    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "datamodel.xml"
        self.path.write_text(_DATAMODEL_XML, encoding="utf-8")
        self.transport = _CapturingTransport({"Person::7": {"key": "7", "fields": {"id": "7"}}})
        self.itop = Itop(
            url="http://mock/rest.php",
            version="1.3",
            auth_user="u",
            auth_pwd="p",
            data_model=str(self.path),
            transport=self.transport,
        )

    async def test_a_nested_lookup_goes_out_as_the_view(self):
        view = self.itop.as_(auth=ItopAuth(token="engineer-token"))

        await view.schema("UserRequest").update({"id": 5}, {"caller_name": "John Doe"})

        self.assertEqual(self.transport.requests[0]["class"], "Person")  # the lookup
        self.assertEqual(self.transport.tokens[0], "engineer-token")
        self.assertEqual(self.transport.tokens[1], "engineer-token")  # the update itself

    async def test_datamodel_attributes_are_rebound_to_the_view(self):
        view = self.itop.as_(auth=ItopAuth(token="engineer-token"))

        self.assertIs(view.UserRequest.itop, view)
        self.assertIs(self.itop.UserRequest.itop, self.itop)


if __name__ == "__main__":
    unittest.main()
