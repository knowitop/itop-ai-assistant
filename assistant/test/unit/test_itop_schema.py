import json
import unittest
from urllib.parse import parse_qs

import httpx

from itop_client import Itop


class _CapturingTransport(httpx.AsyncBaseTransport):
    """Captures the json_data of each request and returns preset objects."""

    def __init__(self, objects: dict | None = None):
        self.requests: list[dict] = []
        self._objects = objects

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = parse_qs(request.content.decode())
        self.requests.append(json.loads(body["json_data"][0]))
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


if __name__ == "__main__":
    unittest.main()
