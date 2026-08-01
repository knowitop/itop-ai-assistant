import json
from copy import copy
from typing import Any, Dict, Optional

import httpx

from .auth import ItopAuth
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
        timeout: float = 30.0,
        comment: Optional[str] = None,
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
        :param timeout: HTTP timeout in seconds for all iTop requests.
        :param comment: Text iTop records in the object's change history for every
                        operation this client makes. None keeps the per-operation
                        default ("Update UserRequest" and friends).
        """
        self.url = url
        self.version = version
        self.auth = ItopAuth(user=auth_user, pwd=auth_pwd, token=auth_token)
        self.comment = comment
        self.data_model: Optional[DataModel] = None
        self._http = httpx.AsyncClient(transport=transport, timeout=timeout)
        # A view built by as_() borrows this pool instead of owning it.
        self._owns_http = True

        if data_model:
            self.data_model = DataModel(data_model)
            self._bind_schemas()

    @property
    def auth_user(self) -> Optional[str]:
        return self.auth.user

    @property
    def auth_pwd(self) -> Optional[str]:
        return self.auth.pwd

    @property
    def auth_token(self) -> Optional[str]:
        return self.auth.token

    def _bind_schemas(self) -> None:
        """Expose the datamodel's classes as attributes bound to *this* client."""
        assert self.data_model is not None
        for schema_name in self.data_model.schemas:
            setattr(self, schema_name, Schema(self, schema_name))

    def as_(self, auth: Optional[ItopAuth] = None, comment: Optional[str] = None) -> "Itop":
        """This client seen through different credentials and/or a different comment.

        The connection pool is shared: iTop authenticates per request, so one
        pool legitimately serves several identities. A view does not own the
        pool — closing it is a no-op, and only the client that built the pool
        can close it.

        Every request a view makes inherits its credentials, including the ones
        `Schema` issues on its own (`update` retrying as multi, `sync` calling
        `update`, `lookup` resolving external keys). That is the point of a view
        over a per-call parameter: a forgotten hand-off there would silently
        resolve someone else's objects under the service account.
        """
        if auth is None and comment is None:
            return self
        view = copy(self)
        view._owns_http = False
        if auth is not None:
            view.auth = auth
        if comment is not None:
            view.comment = comment
        if view.data_model:
            # copy() carried over Schemas bound to *self*; rebind them, or a
            # datamodel attribute would quietly act as the original client.
            view._bind_schemas()
        return view

    async def aclose(self) -> None:
        """Close the underlying HTTP client. A view borrows it and closes nothing."""
        if self._owns_http:
            await self._http.aclose()

    async def check_credentials(self) -> None:
        """Verify credentials against iTop. Raises ItopError if invalid."""
        await self.request({"operation": "core/check_credentials", "user": self.auth.user, "password": self.auth.pwd})

    async def request(self, data: Dict[str, Any], raw_response: bool = False) -> Any:
        """
        Generic async request to iTop REST API.

        :param data: Operation payload (without auth fields).
        :param raw_response: If True, return raw objects dict instead of cleaned list.
        :return: List of objects (dicts with fields + id) or raw dict.
        :raises ItopError: On any iTop or HTTP error.
        """
        form: Dict[str, Any] = {"version": self.version, "json_data": json.dumps(data)}
        if self.auth.user:
            form["auth_user"] = self.auth.user
        if self.auth.pwd:
            form["auth_pwd"] = self.auth.pwd

        headers: Dict[str, str] = {}
        if self.auth.token:
            headers["Auth-Token"] = self.auth.token

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

        return clean_objects

    def schema(self, name: str) -> "Schema":
        """Get a Schema instance for an iTop class by name."""
        return Schema(self, name)
