import json
from typing import Any, Dict, Optional

import httpx

from .datamodel import DataModel
from .exceptions import ItopError
from .schema import Schema


class Itop:
    def __init__(
        self,
        url: str,
        version: str,
        auth_user: Optional[str] = None,
        auth_pwd: Optional[str] = None,
        auth_token: Optional[str] = None,
        data_model: Optional[str] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        """
        Create iTop API client.

        :param url: iTop rest.php endpoint (e.g. http://itop/webservices/rest.php)
        :param version: API version (e.g. "1.3")
        :param auth_user: iTop username (basic auth)
        :param auth_pwd: iTop password (basic auth)
        :param auth_token: iTop auth token, passed as Auth-Token header (alternative to user/pwd)
        :param data_model: Optional path to datamodel XML file.
                           When provided, iTop class schemas are accessible as attributes
                           (e.g. itop.UserRequest, itop.Person).
        :param transport: Optional httpx transport — used for testing (httpx.MockTransport).
        """
        self.url = url
        self.version = version
        self.auth_user = auth_user
        self.auth_pwd = auth_pwd
        self.auth_token = auth_token
        self.data_model: Optional[DataModel] = None
        self._http = httpx.AsyncClient(transport=transport)

        if data_model:
            self.data_model = DataModel(data_model)
            for schema_name in self.data_model.schemas:
                setattr(self, schema_name, Schema(self, schema_name))

    async def check_credentials(self) -> None:
        """Verify credentials against iTop. Raises ItopError if invalid."""
        await self.request({"operation": "core/check_credentials", "user": self.auth_user, "password": self.auth_pwd})

    async def request(self, data: Dict[str, Any], raw_response: bool = False) -> Any:
        """
        Generic async request to iTop REST API.

        :param data: Operation payload (without auth fields).
        :param raw_response: If True, return raw objects dict instead of cleaned list.
        :return: List of objects (dicts with fields + id) or raw dict.
        :raises ItopError: On any iTop or HTTP error.
        """
        form: Dict[str, Any] = {"version": self.version, "json_data": json.dumps(data)}
        if self.auth_user:
            form["auth_user"] = self.auth_user
        if self.auth_pwd:
            form["auth_pwd"] = self.auth_pwd

        headers: Dict[str, str] = {}
        if self.auth_token:
            headers["Auth-Token"] = self.auth_token

        try:
            response = await self._http.post(self.url, data=form, headers=headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise ItopError(e.response.status_code, str(e)) from e
        except httpx.RequestError as e:
            raise ItopError(0, f"Network error: {e}") from e

        json_return = response.json()
        return_code = json_return.get("code", -1)

        if return_code != 0:
            raise ItopError(return_code, json_return.get("message", "Unknown error"))

        if "objects" not in json_return or json_return["objects"] is None:
            return []

        if raw_response:
            return json_return["objects"]

        clean_objects = [{**obj["fields"], "id": obj["key"]} for obj in json_return["objects"].values()]

        if "output_fields" in data:
            if len(data["output_fields"].split(", ")) > 1 and data["output_fields"] != "*":
                clean_objects = []

        return clean_objects

    def schema(self, name: str) -> "Schema":
        """Get a Schema instance for an iTop class by name."""
        return Schema(self, name)
